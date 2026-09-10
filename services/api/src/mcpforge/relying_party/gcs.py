"""Read a delivered attestation token from Cloud Storage — F8-02, API side.

The relying party reads `gs://<bucket>/attestation/<run_id>.jwt` with its own
Application Default Credentials (the owner's, locally) over the Cloud Storage
JSON API. No key file, and no client library: `google-auth` and `httpx` are
already dependencies of this service, and one authenticated GET needs nothing
more. Implements `DeliveredTokenSource`.

The access token is sent only in the `Authorization` header and appears in no
log or message. The body is capped at `MAX_TOKEN_BYTES` while streaming, so an
oversized object is refused without being read in full.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import httpx

from mcpforge.execution.confidential_space import MAX_TOKEN_BYTES, DeliveryUnreadableError

READ_ONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
STORAGE_API_BASE = "https://storage.googleapis.com/storage/v1"
READ_TIMEOUT_SECONDS = 20.0


def _application_default_credentials() -> Any:
    import google.auth

    credentials, _project = google.auth.default(scopes=[READ_ONLY_SCOPE])
    return credentials


class GcsDeliveredTokenSource:
    """`DeliveredTokenSource` over the Cloud Storage JSON API."""

    def __init__(
        self,
        bucket: str,
        *,
        credentials: Any | None = None,
        client: httpx.Client | None = None,
        api_base: str = STORAGE_API_BASE,
    ) -> None:
        if not bucket.strip():
            raise ValueError("an attestation bucket is required")
        self._bucket = bucket
        self._credentials = credentials
        self._client = client
        self._api_base = api_base

    def _bearer(self) -> str:
        import google.auth.exceptions

        # A credential failure is a named refusal, not an exception escaping the
        # verification path. Only the exception type is recorded.
        try:
            if self._credentials is None:
                self._credentials = _application_default_credentials()
            credentials = self._credentials
            if not getattr(credentials, "valid", False):
                import google.auth.transport.requests

                credentials.refresh(google.auth.transport.requests.Request())
        except google.auth.exceptions.GoogleAuthError as exc:
            raise DeliveryUnreadableError(
                f"Application Default Credentials failed: {type(exc).__name__}"
            ) from exc
        token = getattr(credentials, "token", None)
        if not isinstance(token, str) or not token:
            raise DeliveryUnreadableError("no usable Application Default Credentials")
        return token

    def fetch(self, object_name: str) -> bytes | None:
        url = (
            f"{self._api_base}/b/{urllib.parse.quote(self._bucket, safe='')}"
            f"/o/{urllib.parse.quote(object_name, safe='')}"
        )
        client = self._client or httpx.Client(timeout=READ_TIMEOUT_SECONDS)
        try:
            with client.stream(
                "GET",
                url,
                params={"alt": "media"},
                headers={"Authorization": f"Bearer {self._bearer()}"},
            ) as response:
                if response.status_code == httpx.codes.NOT_FOUND:
                    return None
                if response.status_code != httpx.codes.OK:
                    raise DeliveryUnreadableError(
                        f"Cloud Storage answered HTTP {response.status_code}"
                    )
                received = bytearray()
                for chunk in response.iter_bytes():
                    received += chunk
                    if len(received) > MAX_TOKEN_BYTES:
                        raise DeliveryUnreadableError(
                            f"the object is larger than {MAX_TOKEN_BYTES} bytes"
                        )
                return bytes(received)
        except httpx.HTTPError as exc:
            raise DeliveryUnreadableError(
                f"could not reach Cloud Storage: {type(exc).__name__}"
            ) from exc
        finally:
            if self._client is None:
                client.close()
