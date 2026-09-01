"""Health endpoint — reports real state, never optimistic state."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mcpforge.config import Settings
from mcpforge.main import create_app


def test_healthz_reports_real_capability_state(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["configured"]["authentication"] is True
    # No Gemini key in the test settings, and the endpoint says so.
    assert body["configured"]["gemini"] is False


def test_healthz_never_claims_hardware_attestation(settings: Settings) -> None:
    """03_SECURITY_ACCESS.md §2 — attestation is not implemented and not simulated."""
    with TestClient(create_app(settings)) as client:
        body = client.get("/healthz").json()
    assert body["hardware_attested"] is False
    assert body["secure_execution"] == "development"


def test_unconfigured_auth_is_reported_not_hidden(unconfigured_settings: Settings) -> None:
    with TestClient(create_app(unconfigured_settings)) as client:
        body = client.get("/healthz").json()
    assert body["configured"]["authentication"] is False


def test_authenticated_route_returns_503_when_auth_unconfigured(
    unconfigured_settings: Settings,
) -> None:
    """An unconfigured deployment says so; it does not imply a bad credential."""
    with TestClient(create_app(unconfigured_settings)) as client:
        response = client.get("/api/me", headers={"Authorization": "Bearer x"})
    assert response.status_code == 503


def test_health_is_public(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
