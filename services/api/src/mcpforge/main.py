"""MCPForge API application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mcpforge.api import (
    agent,
    approvals,
    attestation,
    chat,
    generation,
    health,
    me,
    pipeline,
    projects,
    repos,
    trust,
)
from mcpforge.auth.firebase import FirebaseIdTokenVerifier
from mcpforge.auth.identity import TokenVerifier
from mcpforge.config import ConfigError, SecureExecutorKind, Settings, StoreKind, get_settings
from mcpforge.execution.development import DevelopmentSecureExecutor
from mcpforge.execution.provider import SecureExecutionProvider
from mcpforge.gemini.google_provider import GoogleGenAIProvider
from mcpforge.gemini.provider import GeminiProvider
from mcpforge.github.client import GitHubAppClient
from mcpforge.logging import configure_logging, get_logger
from mcpforge.orchestration.pipeline import PipelineOptions
from mcpforge.store.firestore import FirestoreStore
from mcpforge.store.memory import InMemoryStore
from mcpforge.store.port import Store

VERSION = "0.1.0"

StartupCheck = Callable[[], Awaitable[None]]


def build_default_store(settings: Settings) -> tuple[Store, StartupCheck | None]:
    """The store this deployment runs with, and the check it must pass at startup.

    - `memory`: `InMemoryStore`. Nothing survives a restart; no check.
    - `firestore`: `FirestoreStore` on FIREBASE_PROJECT_ID, with ADC. An unset
      project id is a `ConfigError` here, and missing ADC surfaces from the
      constructor as `StoreUnavailableError` — both at `create_app`, so both
      abort import. Reachability needs a network read, so it is returned as a
      check the lifespan awaits before the service accepts traffic.
    """
    if settings.store is StoreKind.FIRESTORE:
        if not settings.firebase_project_id:
            raise ConfigError(
                "STORE=firestore requires FIREBASE_PROJECT_ID, the project whose "
                "Firestore database holds run state. There is no fallback store."
            )
        firestore_store = FirestoreStore(settings.firebase_project_id)
        return firestore_store, firestore_store.check_reachable
    return InMemoryStore(), None


class _Unset(Enum):
    """Distinguishes an omitted `executor` argument from an explicit `None`."""

    OMITTED = "omitted"


def build_default_executor(
    settings: Settings,
) -> tuple[SecureExecutionProvider | None, str | None]:
    """The executor this deployment runs with, and why there is none if not.

    - `confidential_space`: the API is the attestation relying party (F8-02);
      its executor refuses every job and reports an attested level only after
      it has verified a delivered token.
    - `development`: `DevelopmentSecureExecutor`, labelled
      `DEVELOPMENT_ISOLATION` and nothing higher. If this machine cannot deny a
      job the network (no unprivileged network namespaces), nothing is attached
      rather than running jobs unisolated; the pipeline's 503 and the startup
      log state why. Startup is not refused, because `app = create_app()` runs
      at import and hosts without namespaces (CI runners among them) must still
      serve health, auth and the trust panel.
    """
    if settings.secure_executor is SecureExecutorKind.CONFIDENTIAL_SPACE:
        from mcpforge.relying_party import build_executor

        return build_executor(settings), None

    executor = DevelopmentSecureExecutor(
        workspace_root=Path(settings.workspace_root),
        memory_mb=settings.job_memory_mb,
    )
    if not executor.network_isolation_available:
        return None, (
            "this machine cannot create unprivileged network namespaces, so a job "
            "could not be denied the network. MCPForge refuses to run repository "
            "jobs unisolated."
        )
    return executor, None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log = get_logger(__name__)
    log.info(
        "mcpforge.startup",
        environment=settings.mcpforge_env.value,
        auth_configured=settings.auth_configured,
        gemini_configured=settings.gemini_configured,
        github_configured=settings.github_configured,
        secure_executor=settings.secure_executor.value,
        store=type(app.state.store).__name__,
    )
    store_check: StartupCheck | None = app.state.store_startup_check
    if store_check is not None:
        # Raises StoreUnavailableError, which aborts startup: the service never
        # accepts a run it could not persist.
        await store_check()
    if settings.store is StoreKind.MEMORY:
        log.warning(
            "mcpforge.store_ephemeral",
            detail="STORE=memory; runs, approvals and artifacts are lost on restart",
        )
    if not settings.auth_configured:
        # Loud in development, impossible in production (require_production_invariants).
        log.warning(
            "mcpforge.auth_unconfigured",
            detail="FIREBASE_PROJECT_ID is unset; authenticated routes will return 503",
        )
    yield


def create_app(
    settings: Settings | None = None,
    *,
    token_verifier: TokenVerifier | None = None,
    store: Store | None = None,
    gemini: GeminiProvider | None = None,
    github: GitHubAppClient | None = None,
    executor: SecureExecutionProvider | _Unset | None = _Unset.OMITTED,
    pipeline_options: PipelineOptions | None = None,
) -> FastAPI:
    """Build the application.

    `token_verifier` is injectable so tests exercise the real dependency chain
    with a locally signed key rather than mocking authentication away.

    `executor`, when omitted, is built from settings by `build_default_executor`
    (T1). Passing one — or an explicit `None`, meaning "no provider" — wins.
    Without a provider the pipeline routes (`api/pipeline.py`, F9-01) refuse
    with 503 rather than assuming one; the trust panel reports the same absence
    (`api/trust.py`).
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.is_production)

    app = FastAPI(
        title="MCPForge API",
        version=VERSION,
        description=(
            "Backend for MCPForge. Owns all model calls, credentials, repository "
            "access and authorization decisions."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.token_verifier = token_verifier or FirebaseIdTokenVerifier(
        settings.firebase_project_id
    )
    # An explicitly passed store always wins and is not probed; only an omitted
    # one is built from settings (T2).
    store_check: StartupCheck | None = None
    if store is None:
        store, store_check = build_default_store(settings)
    app.state.store = store
    app.state.store_startup_check = store_check
    app.state.gemini = gemini or GoogleGenAIProvider(settings)
    # An explicitly passed executor (including an explicit None) always wins;
    # only an omitted argument is built from settings.
    unavailable_reason: str | None = None
    if isinstance(executor, _Unset):
        executor, unavailable_reason = build_default_executor(settings)
    app.state.executor = executor
    app.state.executor_unavailable_reason = unavailable_reason
    app.state.pipeline_options = pipeline_options or PipelineOptions()
    app.state.github = github or GitHubAppClient(
        app_id=settings.github_app_id,
        private_key_path=settings.github_app_private_key_path,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(projects.router)
    app.include_router(chat.router)
    app.include_router(approvals.router)
    app.include_router(repos.router)
    app.include_router(generation.router)
    app.include_router(pipeline.router)
    app.include_router(agent.router)
    app.include_router(trust.router)
    app.include_router(attestation.router)
    return app


app = create_app()
