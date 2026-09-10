"""The MCPForge API as the relying party for Confidential Space attestation — F8-02.

Issues attestation runs (`runs.py`), reads delivered tokens (`gcs.py`), and
builds the `ConfidentialSpaceSecureExecutor` whose verification is the only one
that counts. Not part of the workload image: the Dockerfile copies
`mcpforge/execution/` and nothing from here.
"""

from __future__ import annotations

from mcpforge.config import ConfigError, Settings
from mcpforge.execution.confidential_space import ConfidentialSpaceSecureExecutor
from mcpforge.relying_party.gcs import GcsDeliveredTokenSource
from mcpforge.relying_party.runs import FileAttestationRunStore


def build_executor(settings: Settings) -> ConfidentialSpaceSecureExecutor:
    """The relying party's executor, from the API's own configuration only.

    The image digest in particular is the API's pin, never a value the
    operator or the workload supplied.
    """
    missing = [
        name
        for name, value in (
            ("CONFIDENTIAL_SPACE_IMAGE_DIGEST", settings.confidential_space_image_digest),
            (
                "CONFIDENTIAL_SPACE_WORKLOAD_SERVICE_ACCOUNT",
                settings.confidential_space_workload_service_account,
            ),
            (
                "CONFIDENTIAL_SPACE_ATTESTATION_BUCKET",
                settings.confidential_space_attestation_bucket,
            ),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "SECURE_EXECUTOR=confidential_space needs "
            + ", ".join(missing)
            + "; there is no default."
        )
    assert settings.confidential_space_image_digest is not None
    assert settings.confidential_space_workload_service_account is not None
    assert settings.confidential_space_attestation_bucket is not None
    return ConfidentialSpaceSecureExecutor(
        runs=FileAttestationRunStore(settings.confidential_space_run_directory),
        tokens=GcsDeliveredTokenSource(settings.confidential_space_attestation_bucket),
        image_digest=settings.confidential_space_image_digest,
        workload_service_account=settings.confidential_space_workload_service_account,
    )
