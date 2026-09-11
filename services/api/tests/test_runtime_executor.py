"""T1 — the running service builds its executor from settings.

The isolation-unavailable case patches the real probe the executor's
constructor calls (`_network_isolation_available`), not the factory, so the
refusal is driven by the same detection the executor itself uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcpforge import main as main_module
from mcpforge.auth.identity import AuthError, VerifiedIdentity
from mcpforge.config import Environment, SecureExecutorKind, Settings
from mcpforge.execution import development
from mcpforge.execution.development import DevelopmentSecureExecutor
from mcpforge.execution.provider import TrustLevel
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.main import build_default_executor, create_app
from mcpforge.store.memory import InMemoryStore
from tests.conftest import TEST_PROJECT


def dev_settings(tmp_path: Path) -> Settings:
    return Settings(
        mcpforge_env=Environment.DEVELOPMENT,
        firebase_project_id=TEST_PROJECT,
        secure_executor=SecureExecutorKind.DEVELOPMENT,
        workspace_root=str(tmp_path / "ws"),
    )


def no_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(development, "_network_isolation_available", lambda: False)


def test_development_setting_attaches_a_development_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(development, "_network_isolation_available", lambda: True)
    app = create_app(dev_settings(tmp_path))

    executor = app.state.executor
    assert isinstance(executor, DevelopmentSecureExecutor)
    assert executor.trust_level is TrustLevel.DEVELOPMENT_ISOLATION
    assert executor.network_isolation_available is True
    assert app.state.executor_unavailable_reason is None


def test_development_executor_uses_the_configured_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(development, "_network_isolation_available", lambda: True)
    executor, _ = build_default_executor(dev_settings(tmp_path))
    assert isinstance(executor, DevelopmentSecureExecutor)
    assert executor._root == tmp_path / "ws"


def test_without_network_isolation_nothing_is_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_isolation(monkeypatch)
    app = create_app(dev_settings(tmp_path))

    assert app.state.executor is None
    reason = app.state.executor_unavailable_reason
    assert reason and "network" in reason


class TokenIsUid:
    async def verify(self, raw_token: str) -> VerifiedIdentity:
        if not raw_token.startswith("uid-"):
            raise AuthError("bad token")
        return VerifiedIdentity(subject=raw_token, issuer="test")


def test_without_network_isolation_the_stage_503_states_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_isolation(monkeypatch)
    app = create_app(
        dev_settings(tmp_path),
        token_verifier=TokenIsUid(),
        store=InMemoryStore(),
        gemini=FakeGeminiProvider([]),
    )
    headers = {"Authorization": "Bearer uid-1"}
    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "demo"}, headers=headers)
        assert project.status_code == 201, project.text
        assert project.json()["is_demo"] is True
        project_id = project.json()["id"]
        session = client.post(f"/api/projects/{project_id}/sessions", headers=headers)
        assert session.status_code == 201, session.text
        session_id = session.json()["id"]
        base = f"/api/sessions/{session_id}/pipeline"
        connected = client.post(f"{base}/connect", json={}, headers=headers)
        assert connected.status_code == 200, connected.text
        # Analysis is the first stage that needs the executor.
        response = client.post(f"{base}/analysis", headers=headers)

    assert response.status_code == 503, response.text
    assert "Secure executor not attached" in response.json()["detail"]
    assert "network" in response.json()["detail"]


def test_confidential_space_still_uses_the_relying_party_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Settings] = []
    sentinel = object()

    def fake_build(settings: Settings) -> object:
        built.append(settings)
        return sentinel

    import mcpforge.relying_party as relying_party

    monkeypatch.setattr(relying_party, "build_executor", fake_build)
    settings = Settings(
        mcpforge_env=Environment.DEVELOPMENT,
        firebase_project_id=TEST_PROJECT,
        secure_executor=SecureExecutorKind.CONFIDENTIAL_SPACE,
    )
    app = create_app(settings)

    assert app.state.executor is sentinel
    assert built == [settings]
    assert not isinstance(app.state.executor, DevelopmentSecureExecutor)


def test_an_explicit_executor_wins(tmp_path: Path) -> None:
    explicit = DevelopmentSecureExecutor(workspace_root=tmp_path / "explicit")
    app = create_app(dev_settings(tmp_path), executor=explicit)
    assert app.state.executor is explicit


def test_an_explicit_none_means_no_executor(tmp_path: Path) -> None:
    app = create_app(dev_settings(tmp_path), executor=None)
    assert app.state.executor is None
    assert app.state.executor_unavailable_reason is None


def test_the_factory_is_what_create_app_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Settings] = []

    def spy(settings: Settings) -> tuple[None, str]:
        calls.append(settings)
        return None, "spy"

    monkeypatch.setattr(main_module, "build_default_executor", spy)
    create_app(dev_settings(tmp_path))
    assert len(calls) == 1
