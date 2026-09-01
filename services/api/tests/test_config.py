"""Environment validation — F1-03 acceptance criteria."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcpforge.config import ConfigError, Environment, SecureExecutorKind, Settings


def test_development_defaults_are_usable_without_configuration() -> None:
    s = Settings(mcpforge_env=Environment.DEVELOPMENT)
    assert s.gemini_model == "gemini-3.7-flash"
    assert s.secure_executor is SecureExecutorKind.DEVELOPMENT


def test_unconfigured_integrations_report_unconfigured_rather_than_defaulting() -> None:
    s = Settings(firebase_project_id=None, gemini_api_key=None)
    assert s.auth_configured is False
    assert s.gemini_configured is False


def test_production_requires_auth_and_gemini() -> None:
    s = Settings(
        mcpforge_env=Environment.PRODUCTION, secure_executor=SecureExecutorKind.CONFIDENTIAL_SPACE
    )
    with pytest.raises(ConfigError) as exc:
        s.require_production_invariants()
    assert "FIREBASE_PROJECT_ID" in str(exc.value)
    assert "GEMINI_API_KEY" in str(exc.value)


def test_production_refuses_development_isolation() -> None:
    """Development isolation must never back a production deployment."""
    s = Settings(
        mcpforge_env=Environment.PRODUCTION,
        firebase_project_id="p",
        gemini_api_key="k",
        secure_executor=SecureExecutorKind.DEVELOPMENT,
    )
    with pytest.raises(ConfigError, match="not permitted in production"):
        s.require_production_invariants()


def test_production_with_everything_set_passes() -> None:
    s = Settings(
        mcpforge_env=Environment.PRODUCTION,
        firebase_project_id="p",
        gemini_api_key="k",
        secure_executor=SecureExecutorKind.CONFIDENTIAL_SPACE,
    )
    s.require_production_invariants()


def test_wildcard_cors_origin_rejected() -> None:
    """A permissive default must not be reachable through configuration."""
    with pytest.raises(ValidationError, match="never permitted"):
        Settings(api_cors_origins="*")


def test_invalid_enum_value_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        Settings(secure_executor="tee-please")  # type: ignore[arg-type]


def test_cors_origins_are_split_and_trimmed() -> None:
    s = Settings(api_cors_origins="http://a.test , http://b.test")
    assert s.cors_origins == ["http://a.test", "http://b.test"]


def test_startup_aborts_when_production_configuration_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real startup path must abort, not just the invariant helper — F1-03.

    get_settings() is what main.py calls, and it is cached, so this exercises the
    path that actually runs when the process boots.
    """
    from mcpforge import config as config_module

    for var in ("FIREBASE_PROJECT_ID", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MCPFORGE_ENV", "production")
    config_module.get_settings.cache_clear()

    try:
        with pytest.raises(ConfigError) as exc:
            config_module.get_settings()
    finally:
        config_module.get_settings.cache_clear()

    message = str(exc.value)
    assert "FIREBASE_PROJECT_ID" in message
    assert "GEMINI_API_KEY" in message


def test_startup_succeeds_in_development_without_optional_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Development must not require credentials — but it must not fake them either."""
    from mcpforge import config as config_module

    monkeypatch.setenv("MCPFORGE_ENV", "development")
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    config_module.get_settings.cache_clear()
    try:
        settings = config_module.get_settings()
        assert settings.auth_configured is False
    finally:
        config_module.get_settings.cache_clear()


def test_tests_do_not_inherit_local_developer_configuration() -> None:
    """Guards the isolation fixture itself.

    A real key in the developer's .env once made two tests fail — and could
    equally have made an "unconfigured" test pass for the wrong reason. This
    asserts the default test Settings sees nothing from the environment.
    """
    s = Settings()
    assert s.gemini_api_key is None
    assert s.firebase_project_id is None
    assert s.gemini_configured is False
    assert s.auth_configured is False
