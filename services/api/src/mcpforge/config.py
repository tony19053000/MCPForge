"""Environment validation — 05_FEATURE_TICKETS.md F1-03.

Configuration fails fast and loudly. A missing required variable aborts startup
with a message naming it; an unconfigured optional integration reports itself as
unconfigured. No default value may weaken a security control, and nothing here
silently substitutes a permissive fallback for a missing setting.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class GeminiBackend(StrEnum):
    """How the Gemini SDK authenticates.

    `API_KEY` uses a key from Google AI Studio. `VERTEX` uses Application
    Default Credentials against a GCP project, which needs no secret at all —
    the preferred path where organization policy restricts key material.
    """

    API_KEY = "api_key"
    VERTEX = "vertex"


class SecureExecutorKind(StrEnum):
    DEVELOPMENT = "development"
    CONFIDENTIAL_SPACE = "confidential_space"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid. Never caught to continue."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Repo-root .env first, then a service-local override if one exists.
        env_file=("../../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    mcpforge_env: Environment = Environment.DEVELOPMENT
    log_level: str = "info"

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_cors_origins: str = "http://localhost:3000"

    # Gemini. Absent means "unconfigured", never a stub.
    gemini_backend: GeminiBackend = GeminiBackend.API_KEY
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.7-flash"
    gemini_timeout_seconds: int = 120
    gemini_max_retries: int = 2
    # Base for exponential backoff. Set to 0 in tests so retries do not sleep.
    gemini_retry_base_delay_seconds: float = 1.0

    # Identity. Provisional Firebase Auth (02_ARCHITECTURE.md §3.2).
    # Verification needs no credentials — only the project id, to derive the
    # expected issuer and audience.
    firebase_project_id: str | None = None

    # Server-side Google credentials come from ADC. There is deliberately no
    # setting for a service-account key path: key files are unsupported.
    google_cloud_project: str | None = None
    google_cloud_quota_project: str | None = None
    google_cloud_location: str = "us-central1"

    # GitHub App. The private key path points outside the repository, and the
    # key is read at use time and never persisted or logged.
    github_app_id: str | None = None
    github_app_client_id: str | None = None
    github_app_client_secret: str | None = None
    github_app_private_key_path: str | None = None

    secure_executor: SecureExecutorKind = SecureExecutorKind.DEVELOPMENT
    workspace_root: str = "/tmp/mcpforge-workspaces"  # noqa: S108
    job_timeout_seconds: int = 600
    job_memory_mb: int = 2048

    index_max_file_bytes: int = 262_144
    index_max_files: int = 20_000

    @field_validator("api_cors_origins")
    @classmethod
    def _reject_wildcard_origin(cls, v: str) -> str:
        if "*" in v:
            raise ValueError("api_cors_origins must list explicit origins; '*' is never permitted")
        return v

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.mcpforge_env is Environment.PRODUCTION

    @property
    def auth_configured(self) -> bool:
        """Whether identity verification can actually run. Never assumed true."""
        return bool(self.firebase_project_id)

    @property
    def github_configured(self) -> bool:
        """Whether repository access can actually be requested. Never assumed."""
        return bool(self.github_app_id and self.github_app_private_key_path)

    @property
    def gemini_configured(self) -> bool:
        """Whether a real model call can be made. Never assumed true.

        The API-key backend needs a key; the Vertex backend needs a project and
        relies on ambient Application Default Credentials.
        """
        if self.gemini_backend is GeminiBackend.VERTEX:
            return bool(self.google_cloud_project)
        return bool(self.gemini_api_key)

    def require_production_invariants(self) -> None:
        """Settings that are optional in development but mandatory in production.

        Called at startup. Raises rather than degrading, so a production deploy
        cannot come up with authentication or the model provider silently absent.
        """
        missing: list[str] = []
        if not self.firebase_project_id:
            missing.append("FIREBASE_PROJECT_ID")
        if not self.gemini_configured:
            missing.append(
                "GOOGLE_CLOUD_PROJECT (GEMINI_BACKEND=vertex)"
                if self.gemini_backend is GeminiBackend.VERTEX
                else "GEMINI_API_KEY"
            )
        if missing:
            raise ConfigError(
                "Missing required configuration for production: "
                + ", ".join(missing)
                + ". Set them in the environment; there is no fallback."
            )
        if self.secure_executor is SecureExecutorKind.DEVELOPMENT:
            raise ConfigError(
                "SECURE_EXECUTOR=development is not permitted in production. "
                "Development isolation must never back a production deployment."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:  # pragma: no cover - exercised via load_settings
        raise ConfigError(f"Invalid MCPForge configuration:\n{exc}") from exc
    if settings.is_production:
        settings.require_production_invariants()
    return settings
