"""The auth port must be genuinely provider-agnostic — F1-06 acceptance criteria.

02_ARCHITECTURE.md §3.2 promises that swapping identity providers means writing
one new TokenVerifier and changing nothing else. These tests hold us to it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi.testclient import TestClient

from mcpforge.auth.identity import AuthError, TokenVerifier, VerifiedIdentity
from mcpforge.config import Settings
from mcpforge.main import create_app

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def test_backend_imports_no_firebase_sdk() -> None:
    """No vendor SDK anywhere in the backend.

    Verification is done against Google's public JWKS with PyJWT, which needs no
    credentials — and organization policy blocks service-account keys anyway.
    Reintroducing firebase-admin would break both.
    """
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and (
                "firebase_admin" in stripped or "google.cloud.firestore" in stripped
            ):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {stripped}")
    assert not offenders, "Vendor SDK imported in backend:\n" + "\n".join(offenders)


class StaticVerifier:
    """A second, unrelated TokenVerifier implementation.

    Stands in for the direct-Google-OAuth verifier we expect to write later. If
    the port ever leaks a Firebase type, this class stops satisfying it.
    """

    def __init__(self, subject: str) -> None:
        self._subject = subject

    async def verify(self, raw_token: str) -> VerifiedIdentity:
        if raw_token != "good":
            raise AuthError("nope")
        return VerifiedIdentity(
            subject=self._subject,
            email="other@example.com",
            email_verified=True,
            issuer="https://accounts.google.com",
        )


def test_a_second_verifier_satisfies_the_port() -> None:
    verifier: TokenVerifier = StaticVerifier("sub-1")
    assert isinstance(verifier, TokenVerifier)


def test_app_works_with_a_different_verifier(settings: Settings) -> None:
    """The API layer depends on the port, not on Firebase."""
    app = create_app(settings, token_verifier=StaticVerifier("sub-42"))
    with TestClient(app) as client:
        response = client.get("/api/me", headers={"Authorization": "Bearer good"})
        assert response.status_code == 200
        assert response.json()["subject"] == "sub-42"
        assert response.json()["issuer"] == "https://accounts.google.com"


def _code_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Executable lines only — comments and docstrings are prose, not behaviour."""
    tree = ast.parse(path.read_text())
    docstrings = {
        node.body[0].value.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    out: list[tuple[int, str]] = []
    in_docstring = False
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if lineno in docstrings:
            in_docstring = True
        if in_docstring:
            if raw.count('"""') >= (2 if lineno in docstrings else 1):
                in_docstring = False
            continue
        if line:
            out.append((lineno, line))
    return out


def test_no_service_account_key_is_configured_or_read() -> None:
    """No code path reads a service-account key file — 03_SECURITY_ACCESS.md §9.

    Server-side Google credentials come from ADC. There is deliberately no
    setting for a key path, and nothing opens one. Checks executable lines only,
    so documentation explaining *why* we avoid key files does not trip it.
    """
    banned = ("GOOGLE_APPLICATION_CREDENTIALS", "google_application_credentials")
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        for lineno, line in _code_lines(path):
            for needle in banned:
                if needle in line:
                    offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line}")
    assert not offenders, "Service-account key material in code:\n" + "\n".join(offenders)


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "bearer"])
def test_bad_authorization_headers_rejected(settings: Settings, header: str | None) -> None:
    app = create_app(settings, token_verifier=StaticVerifier("sub"))
    with TestClient(app) as client:
        headers = {} if header is None else {"Authorization": header}
        assert client.get("/api/me", headers=headers).status_code == 401
