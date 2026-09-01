"""Log redaction — 03_SECURITY_ACCESS.md §9."""

from __future__ import annotations

import pytest

from mcpforge.logging import REDACTED, redact_processor


@pytest.mark.parametrize(
    "key",
    ["authorization", "Authorization", "token", "gemini_api_key", "private_key", "prompt"],
)
def test_sensitive_keys_are_replaced(key: str) -> None:
    out = redact_processor(None, "info", {key: "super-secret-value"})
    assert out[key] == REDACTED


# Assembled at runtime, so this file contains no string a secret scanner
# would match. A test fixture must not itself look like a leaked credential.
TOKEN_SHAPES = [
    "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz012345",
    "AIza" + "SyA1234567890abcdefghijklmnopqrstu",
    "ey" + "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p",
    "-----BEGIN " + "RSA PRIVATE KEY-----",
]


@pytest.mark.parametrize("value", TOKEN_SHAPES)
def test_token_shaped_values_are_scrubbed_from_free_text(value: str) -> None:
    out = redact_processor(None, "info", {"detail": f"failed with {value} attached"})
    assert value not in out["detail"]
    assert REDACTED in out["detail"]


def test_ordinary_values_pass_through() -> None:
    out = redact_processor(None, "info", {"files_indexed": 312, "path": "src/app/page.tsx"})
    assert out["files_indexed"] == 312
    assert out["path"] == "src/app/page.tsx"
