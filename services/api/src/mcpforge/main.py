"""MCPForge API application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mcpforge.api import approvals, chat, health, me, projects, repos
from mcpforge.auth.firebase import FirebaseIdTokenVerifier
from mcpforge.auth.identity import TokenVerifier
from mcpforge.config import Settings, get_settings
from mcpforge.gemini.google_provider import GoogleGenAIProvider
from mcpforge.gemini.provider import GeminiProvider
from mcpforge.github.client import GitHubAppClient
from mcpforge.logging import configure_logging, get_logger
from mcpforge.store.memory import InMemoryStore
from mcpforge.store.port import Store

VERSION = "0.1.0"


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
) -> FastAPI:
    """Build the application.

    `token_verifier` is injectable so tests exercise the real dependency chain
    with a locally signed key rather than mocking authentication away.
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
    # In-memory is the Phase 2 store. Firestore lands behind the same port later.
    app.state.store = store or InMemoryStore()
    app.state.gemini = gemini or GoogleGenAIProvider(settings)
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
    return app


app = create_app()
