"""Deliver an attestation token from the workload to the relying party — F8-02.

Runs inside the Confidential Space workload image. It writes the raw token to a
private Cloud Storage object, `gs://<bucket>/attestation/<run_id>.jwt`, and does
nothing else with it. The token is signed by Google, so this channel does not
need to be trusted: the relying party (the MCPForge API) verifies what it reads.

**This module is transport, and it is kept apart from attestation on purpose.**
Writing to Cloud Storage needs an OAuth access token for the workload's service
account, and that comes from the Compute metadata server — the one place in
MCPForge that talks to it. It is *not* the attestation mechanism: the
attestation token comes only from the Confidential Space launcher, in
`confidential_space.request_attestation_token`, and this module neither
requests nor verifies one; it is handed a token and writes it.
`test_the_metadata_server_is_reached_only_by_the_delivery_transport` pins that
the metadata server appears nowhere else and that the identity endpoint (a VM
identity token, which would be a counterfeit attestation) appears nowhere.

**Why the stdlib JSON API rather than a client library.** Two HTTP calls do not
justify putting `google-cloud-storage` and its dependency closure inside the
attested image, where every byte is inside the trust boundary and pinned by the
digest. `urllib` is already there.

Neither the access token nor the attestation token is logged, raised or
returned in any message; failures carry an HTTP status or an exception type.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from enum import StrEnum

from mcpforge.execution.confidential_space import AttestationToken, run_object_name

#: The workload service account's OAuth access token. Requires the VM to be
#: created with `--scopes=cloud-platform`, which `launch.sh` does.
_METADATA_BASE = "http://metadata.google.internal/computeMetadata/v1"
METADATA_ACCESS_TOKEN_URL = _METADATA_BASE + "/instance/service-accounts/default/token"
#: Cloud Storage JSON API, simple media upload.
STORAGE_UPLOAD_BASE = "https://storage.googleapis.com/upload/storage/v1"
DELIVERY_TIMEOUT_SECONDS = 20.0
MAX_METADATA_RESPONSE_BYTES = 16 * 1024


class DeliveryFailure(StrEnum):
    ACCESS_TOKEN_UNAVAILABLE = "ACCESS_TOKEN_UNAVAILABLE"  # noqa: S105
    ALREADY_DELIVERED = "ALREADY_DELIVERED"
    UPLOAD_REFUSED = "UPLOAD_REFUSED"
    UPLOAD_FAILED = "UPLOAD_FAILED"


class DeliveryError(Exception):
    """The token was not delivered. The message never contains a token."""

    def __init__(self, failure: DeliveryFailure, detail: str) -> None:
        super().__init__(f"{failure.value}: {detail}")
        self.failure = failure
        self.detail = detail


def fetch_access_token(
    *, url: str = METADATA_ACCESS_TOKEN_URL, timeout: float = DELIVERY_TIMEOUT_SECONDS
) -> str:
    """The workload service account's access token, for writing one object."""
    request = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw: bytes = response.read(MAX_METADATA_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise DeliveryError(
            DeliveryFailure.ACCESS_TOKEN_UNAVAILABLE,
            f"the metadata server did not answer: {type(exc).__name__}",
        ) from exc
    try:
        if len(raw) > MAX_METADATA_RESPONSE_BYTES:
            raise ValueError("oversized")
        document = json.loads(raw)
        access = document["access_token"]
        if not isinstance(access, str) or not access:
            raise ValueError("empty")
    except (ValueError, KeyError, TypeError) as exc:
        raise DeliveryError(
            DeliveryFailure.ACCESS_TOKEN_UNAVAILABLE,
            "the metadata server's answer carried no usable access token",
        ) from exc
    return access


def deliver_token(
    token: AttestationToken,
    *,
    bucket: str,
    run_id: str,
    access_token: Callable[[], str] = fetch_access_token,
    upload_base: str = STORAGE_UPLOAD_BASE,
    timeout: float = DELIVERY_TIMEOUT_SECONDS,
) -> str:
    """Create `attestation/<run_id>.jwt` in `bucket` holding exactly the token.

    `ifGenerationMatch=0` makes it create-only: if the object already exists
    the upload is refused (`ALREADY_DELIVERED`) rather than overwriting, which
    `roles/storage.objectCreator` could not do in any case. Returns the
    `gs://` URL written.
    """
    object_name = run_object_name(run_id)
    query = urllib.parse.urlencode(
        {"uploadType": "media", "name": object_name, "ifGenerationMatch": "0"}
    )
    url = f"{upload_base}/b/{urllib.parse.quote(bucket, safe='')}/o?{query}"
    request = urllib.request.Request(  # noqa: S310
        url,
        data=token.value.encode("ascii"),
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token()}",
            "Content-Type": "application/jwt",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise DeliveryError(
            DeliveryFailure.UPLOAD_FAILED, f"Cloud Storage did not answer: {type(exc).__name__}"
        ) from exc

    if status == 200:
        return f"gs://{bucket}/{object_name}"
    if status == 412:
        raise DeliveryError(
            DeliveryFailure.ALREADY_DELIVERED, "an object for this run already exists"
        )
    if status in (401, 403):
        raise DeliveryError(DeliveryFailure.UPLOAD_REFUSED, f"Cloud Storage answered {status}")
    raise DeliveryError(DeliveryFailure.UPLOAD_FAILED, f"Cloud Storage answered {status}")
