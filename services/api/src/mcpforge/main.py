"""MCPForge API application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mcpforge.api import health, me
from mcpforge.auth.firebase import FirebaseIdTokenVerifier
from mcpforge.auth.identity import TokenVerifier
from mcpforge.config import Settings, get_settings
from mcpforge.logging import configure_logging, get_logger

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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(health.router)
    app.include_router(me.router)
    return app


app = create_app()
