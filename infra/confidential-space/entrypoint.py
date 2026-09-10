"""MCPForge Confidential Space workload entrypoint — F8-02a, extended by F8-02.

This is the process that Confidential Space launches inside the attested VM.
Everything in this image sits **inside** the trust boundary: the image digest is
what the relying party pins, so any file baked in here is vouched for by the
attestation.

What it does, in order, and nothing more:

1. Reads its configuration — the run id and audience the relying party issued —
   and **refuses to start** if either is absent. There are no defaults.
2. Refuses to run as root.
3. Refuses to run if credential-shaped material is reachable.
4. Confirms the path jail root exists and is writable.
5. Confirms the payload it carries imports.
6. **Requests an attestation token** from the Confidential Space launcher with
   the issued audience, and **writes the raw token** to the private object
   `gs://mcpforge-aa5c2-attestation/attestation/<run_id>.jwt`.
7. Prints one line of JSON and exits.

What it does **not** do, stated so no reader has to infer it:

- **It does not verify the token.** A workload vouching for itself proves
  nothing to anyone — a malicious image would simply report success. The
  MCPForge API, outside the TEE, fetches the object and verifies it against the
  audience it issued and the digest it pins. Exit 0 here therefore means only
  "a token was obtained and delivered"; it is a self-report, not attestation.
- It does not print, log or store the token anywhere but that object. The JSON
  carries a 16-character SHA-256 prefix and the length.
- It does not run a repository job; there is no job runner in this image.

**Observability on a real VM.** The image sets `log_redirect=never`, so stdout
never leaves the TEE. The launcher reports the exit status on the serial console
as "workload task ended and returned N", which is why every refusal has its own
code.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcpforge.execution.confidential_space import AttestationToken

#: Exit codes. 0 means every precondition held and a token was delivered — a
#: self-report, not attestation. Everything else is a refusal.
EXIT_OK = 0
EXIT_CONFIG_MISSING = 10
EXIT_CONFIG_INVALID = 11
EXIT_RUNNING_AS_ROOT = 12
EXIT_CREDENTIAL_PRESENT = 13
EXIT_WORKSPACE_UNUSABLE = 14
EXIT_WORKLOAD_PAYLOAD_MISSING = 15
#: No token was obtained from the launcher (socket missing, refused, timed out,
#: non-200, or a body that is not exactly one JWS).
EXIT_ATTESTATION_UNAVAILABLE = 16
#: A token was obtained and could not be delivered to the relying party.
EXIT_DELIVERY_FAILED = 17

#: Configuration this workload requires. Absent or blank is a refusal. Both are
#: issued by the relying party (the MCPForge API), passed by `launch.sh` as
#: `tee-env-*` metadata, and are the only names in
#: `tee.launch_policy.allow_env_override`;
#: `test_every_required_name_is_supplied_by_exactly_one_source` checks, against
#: the built image and the launch plan, that nothing here goes unsupplied.
REQUIRED_ENVIRONMENT: tuple[str, ...] = (
    # Correlates this run with the orchestrator's run. Becomes a directory name,
    # so its shape is constrained.
    "MCPFORGE_RUN_ID",
    # The nonce the relying party chose, which the token must carry as its
    # single audience. The relying party verifies against its own record of it,
    # so a value it did not issue produces a token nobody will accept.
    "MCPFORGE_ATTESTATION_AUDIENCE",
)

#: The path jail root. **A constant, not configuration**, for two reasons.
#:
#: 1. It must not be operator-settable — an operator who could point it at `/`
#:    would move the jail rather than escape it, the same outcome by a politer
#:    route. It was excluded from `allow_env_override` for that reason, and
#:    that exclusion stands.
#: 2. As an image `ENV` it depended on the launcher propagating the image's
#:    environment. The launcher's source (`oci.WithImageConfigArgs` in
#:    `google/go-tpm-tools` `launcher/container_spec.go`) does propagate it, but
#:    the failed VM run reported this name missing, and a safety root should not
#:    rest on a behaviour we could not observe. A constant has the same value
#:    and the same non-overridability with no dependency at all.
#:
#: It is still checked, not assumed: `check_workspace` refuses a missing or
#: unwritable root, exactly as before.
WORKSPACE_ROOT = Path("/workspace")

#: Names that are no longer configuration. Present at all — even blank — is a
#: refusal, so a launch that still tries to relocate the jail is told so rather
#: than silently ignored.
RETIRED_ENVIRONMENT: tuple[str, ...] = ("MCPFORGE_WORKSPACE_ROOT",)

#: The private bucket the token is delivered to. A constant, not configuration:
#: an operator who could redirect it could at most withhold the token, but there
#: is no reason to give them the lever. `setup.sh` creates it and grants the
#: workload service account `roles/storage.objectCreator` on it and nothing else.
ATTESTATION_BUCKET = "mcpforge-aa5c2-attestation"

#: A run id becomes an object name, so it is constrained rather than trusted.
#: The same pattern as `confidential_space.RUN_ID_SHAPE` — this copy exists
#: because it must refuse before the payload is imported —
#: and `test_the_run_id_rule_is_one_rule` fails if the two differ.
RUN_ID_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: An audience is a nonce, not a label. A short value is almost certainly a
#: placeholder someone left in, and a placeholder audience is a replayable one.
MINIMUM_AUDIENCE_LENGTH = 16

#: Filenames that are credential-shaped by name. This list is deliberately
#: *duplicated* by `test_workload_image.py`, which inspects the built layers
#: with its own list. The runtime check and the build-time check are two
#: independent controls and are not refactored into one; a single shared list
#: would mean one edit disables both.
CREDENTIAL_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".npmrc",
        ".netrc",
        "credentials.json",
        "application_default_credentials.json",
        "service-account.json",
        "id_rsa",
        "id_ecdsa",
        "id_ed25519",
    }
)

#: Suffixes that are credential-shaped by extension.
CREDENTIAL_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx", ".crt")

#: Everything this image ships lives under here: the entrypoint itself and
#: the vendored site-packages. The credential scan walks it in full.
WORKLOAD_ROOT = Path(__file__).resolve().parent

#: Absolute paths that must not exist inside the workload. An ADC file here
#: would be a long-lived Google credential baked into an attested image.
FORBIDDEN_ABSOLUTE_PATHS: tuple[str, ...] = (
    "/root/.config/gcloud/application_default_credentials.json",
    "/home/mcpforge/.config/gcloud/application_default_credentials.json",
    "/run/secrets",
)


class RefusalError(Exception):
    """A precondition failed. Carries the exit code the process will use."""

    def __init__(
        self,
        code: int,
        reason: str,
        detail: str,
        *,
        attestation: dict[str, object] | None = None,
    ) -> None:
        super().__init__(f"{reason}: {detail}")
        self.code = code
        self.reason = reason
        self.detail = detail
        self.attestation = attestation


def read_configuration(environment: dict[str, str]) -> dict[str, str]:
    """Return the required configuration, or refuse.

    Whitespace-only counts as absent, matching the rule `attestation.py` applies
    to token claims. Nothing is defaulted: if a name is missing this raises,
    it does not substitute.
    """

    missing = [name for name in REQUIRED_ENVIRONMENT if not environment.get(name, "").strip()]
    if missing:
        raise RefusalError(
            EXIT_CONFIG_MISSING,
            "CONFIG_MISSING",
            "required configuration absent: " + ", ".join(sorted(missing)),
        )

    retired = [name for name in RETIRED_ENVIRONMENT if name in environment]
    if retired:
        raise RefusalError(
            EXIT_CONFIG_INVALID,
            "CONFIG_INVALID",
            ", ".join(sorted(retired)) + " is not configuration; the path jail root is "
            f"fixed at {WORKSPACE_ROOT} and cannot be moved",
        )

    config = {name: environment[name] for name in REQUIRED_ENVIRONMENT}

    run_id = config["MCPFORGE_RUN_ID"]
    if not RUN_ID_SHAPE.fullmatch(run_id):
        raise RefusalError(
            EXIT_CONFIG_INVALID,
            "CONFIG_INVALID",
            "MCPFORGE_RUN_ID does not match the permitted shape",
        )

    audience = config["MCPFORGE_ATTESTATION_AUDIENCE"]
    if audience != audience.strip():
        raise RefusalError(
            EXIT_CONFIG_INVALID,
            "CONFIG_INVALID",
            "MCPFORGE_ATTESTATION_AUDIENCE has surrounding whitespace; the audience "
            "is compared without normalisation, so a padded value would never match",
        )
    if len(audience) < MINIMUM_AUDIENCE_LENGTH:
        raise RefusalError(
            EXIT_CONFIG_INVALID,
            "CONFIG_INVALID",
            f"MCPFORGE_ATTESTATION_AUDIENCE is shorter than {MINIMUM_AUDIENCE_LENGTH} "
            "characters and so is not a usable per-run nonce",
        )

    return config


def check_not_root() -> None:
    """Refuse to run as uid 0.

    `03_SECURITY_ACCESS.md` §3 requires non-root execution. The Dockerfile sets
    `USER`, but a launch policy that permitted an override, or a future edit
    that dropped the line, would silently undo it — so the process checks.
    """

    if os.geteuid() == 0:
        raise RefusalError(
            EXIT_RUNNING_AS_ROOT,
            "RUNNING_AS_ROOT",
            "the workload is running as uid 0; non-root execution is required",
        )


def _is_credential_shaped(path: Path) -> bool:
    name = path.name.casefold()
    if name in CREDENTIAL_FILENAMES:
        return True
    if name.startswith(".env."):
        return True
    return name.endswith(CREDENTIAL_SUFFIXES)


def check_no_credentials(roots: tuple[Path, ...], environment: dict[str, str]) -> None:
    """Refuse if credential material is reachable inside the trust boundary.

    Case-folded, for the reason recorded in log entry 0006: `.ENV` and `ID_RSA`
    are the same files as their lowercase forms and are credentials either way.
    A matching file is never opened — its name is enough to refuse on.

    **The bound on this check, stated because it would otherwise be overclaimed.**
    This runs as the workload user, so it sees the part of the filesystem that
    user can read. `/root` is mode 0700 on the base image, and a permission
    error there is not evidence of absence — it means this process cannot tell.
    Such a path is skipped here and covered instead by
    `test_no_layer_contains_credential_material`, which unpacks every layer of
    the built image as the build user and therefore sees all of it. The two
    checks are complementary: one covers what the running workload can reach,
    the other covers what the attested artefact actually contains. Any *other*
    `OSError` is a state we cannot account for and is a refusal.
    """

    if environment.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
        raise RefusalError(
            EXIT_CREDENTIAL_PRESENT,
            "CREDENTIAL_PRESENT",
            "GOOGLE_APPLICATION_CREDENTIALS is set; key-file ADC is not a supported "
            "configuration (03_SECURITY_ACCESS.md §9)",
        )

    for forbidden in FORBIDDEN_ABSOLUTE_PATHS:
        try:
            present = Path(forbidden).exists()
        except PermissionError:
            continue
        except OSError as exc:
            raise RefusalError(
                EXIT_CREDENTIAL_PRESENT,
                "CREDENTIAL_STATE_UNKNOWN",
                f"could not determine whether {forbidden} exists: {exc}",
            ) from exc
        if present:
            raise RefusalError(
                EXIT_CREDENTIAL_PRESENT,
                "CREDENTIAL_PRESENT",
                f"credential material present at {forbidden}",
            )

    def unreadable(exc: OSError) -> None:
        # os.walk swallows errors by default. A directory inside the workload
        # that we cannot read is an unknown, and an unknown is a refusal.
        raise RefusalError(
            EXIT_CREDENTIAL_PRESENT,
            "CREDENTIAL_STATE_UNKNOWN",
            f"could not scan {exc.filename}: {exc}",
        ) from exc

    for root in roots:
        if not root.is_dir():
            continue
        for directory, _subdirectories, filenames in os.walk(root, onerror=unreadable):
            for filename in filenames:
                if _is_credential_shaped(Path(filename)):
                    raise RefusalError(
                        EXIT_CREDENTIAL_PRESENT,
                        "CREDENTIAL_PRESENT",
                        "credential-shaped file baked into the image: "
                        f"{Path(directory) / filename}",
                    )


def check_workspace(workspace_root: Path) -> None:
    """The path jail root must exist, be a directory, and be writable by us."""

    if not workspace_root.is_dir():
        raise RefusalError(
            EXIT_WORKSPACE_UNUSABLE,
            "WORKSPACE_UNUSABLE",
            f"{workspace_root} is not a directory",
        )
    if not os.access(workspace_root, os.W_OK | os.X_OK):
        raise RefusalError(
            EXIT_WORKSPACE_UNUSABLE,
            "WORKSPACE_UNUSABLE",
            f"{workspace_root} is not writable by the workload user",
        )


def check_workload_payload() -> str:
    """Confirm the secure executor this image claims to carry actually imports.

    The class is imported, never constructed here: constructing it is the
    attestation step below, which has its own refusals.
    """

    try:
        from mcpforge.execution.confidential_space import ConfidentialSpaceSecureExecutor
    except ImportError as exc:
        raise RefusalError(
            EXIT_WORKLOAD_PAYLOAD_MISSING,
            "WORKLOAD_PAYLOAD_MISSING",
            f"the secure executor payload did not import: {exc}",
        ) from exc

    executor = ConfidentialSpaceSecureExecutor
    return f"{executor.__module__}.{executor.__qualname__}"


def preflight(
    environment: dict[str, str], *, workspace_root: Path = WORKSPACE_ROOT
) -> tuple[dict[str, str], dict[str, object]]:
    """Run every precondition in order; return the configuration and a record.

    Raises `RefusalError` on the first failure. Configuration is read first, so
    an operator who supplied nothing is told that rather than being told about
    something downstream. `workspace_root` is a parameter so the host tests can
    point it at a temporary directory; `main` always passes `WORKSPACE_ROOT`.
    """

    config = read_configuration(environment)
    check_not_root()
    check_no_credentials((WORKLOAD_ROOT, Path.home()), environment)
    check_workspace(workspace_root)
    workload = check_workload_payload()

    record: dict[str, object] = {
        "run_id": config["MCPFORGE_RUN_ID"],
        "workspace_root": str(workspace_root),
        "workload": workload,
        "uid": os.geteuid(),
        "python": sys.version.split()[0],
        "job_runner_present": False,
    }
    return config, record


def attest(
    config: dict[str, str],
    *,
    socket_path: Path | None = None,
    deliver: Callable[[AttestationToken, str], str] | None = None,
) -> dict[str, object]:
    """Obtain a token with the issued audience and deliver it. Verify nothing.

    `socket_path` and `deliver` exist so tests can point the real launcher
    client at a local server implementing its documented contract and replace
    Cloud Storage with a local stand-in. `main` passes neither: production uses
    the launcher's socket and `deliver_token` into `ATTESTATION_BUCKET`.
    """

    from mcpforge.execution.confidential_space import (
        TEE_SERVER_SOCKET,
        InvalidAudienceError,
        TokenRetrievalError,
        check_launcher_audience,
        request_attestation_token,
    )
    from mcpforge.execution.token_delivery import DeliveryError, deliver_token

    run_id = config["MCPFORGE_RUN_ID"]
    audience = config["MCPFORGE_ATTESTATION_AUDIENCE"]
    try:
        check_launcher_audience(audience)
    except InvalidAudienceError as exc:
        raise RefusalError(EXIT_CONFIG_INVALID, "CONFIG_INVALID", str(exc)) from exc

    record: dict[str, object] = {
        "token_obtained": False,
        "token_sha256_prefix": None,
        "token_length": None,
        "retrieval_failure": None,
        "delivered_to": None,
        "delivery_failure": None,
    }
    try:
        token = request_attestation_token(
            audience, socket_path=socket_path if socket_path is not None else TEE_SERVER_SOCKET
        )
    except TokenRetrievalError as exc:
        record["retrieval_failure"] = exc.failure.value
        raise RefusalError(
            EXIT_ATTESTATION_UNAVAILABLE,
            "ATTESTATION_UNAVAILABLE",
            f"no attestation token was obtained: {exc.failure.value}",
            attestation=record,
        ) from exc

    fingerprint = token.fingerprint
    record["token_obtained"] = True
    record["token_sha256_prefix"] = fingerprint.sha256_prefix
    record["token_length"] = fingerprint.length

    def deliver_to_bucket(held: AttestationToken, run: str) -> str:
        return deliver_token(held, bucket=ATTESTATION_BUCKET, run_id=run)

    try:
        record["delivered_to"] = (deliver or deliver_to_bucket)(token, run_id)
    except DeliveryError as exc:
        record["delivery_failure"] = exc.failure.value
        raise RefusalError(
            EXIT_DELIVERY_FAILED,
            "DELIVERY_FAILED",
            f"the token was not delivered to the relying party: {exc.failure.value}",
            attestation=record,
        ) from exc
    return record


def _refused(refusal: RefusalError) -> dict[str, object]:
    refused: dict[str, object] = {
        "status": "REFUSED",
        "ok": False,
        "reason": refusal.reason,
        "detail": refusal.detail,
        "exit_code": refusal.code,
    }
    if refusal.attestation is not None:
        refused["attestation"] = refusal.attestation
    return refused


def execute(
    environment: dict[str, str],
    *,
    workspace_root: Path = WORKSPACE_ROOT,
    socket_path: Path | None = None,
    deliver: Callable[[AttestationToken, str], str] | None = None,
) -> tuple[int, dict[str, object]]:
    """Everything `main` does except writing: the exit code and the JSON record.

    A refusal at the attestation step also carries the preflight record, so the
    output shows that every earlier check passed first.
    """

    try:
        config, record = preflight(environment, workspace_root=workspace_root)
    except RefusalError as refusal:
        return refusal.code, _refused(refusal)

    try:
        record["attestation"] = attest(config, socket_path=socket_path, deliver=deliver)
    except RefusalError as refusal:
        refused = _refused(refusal)
        refused["preflight"] = record
        return refusal.code, refused

    record["status"] = "TOKEN_DELIVERED"
    record["ok"] = True
    record["note"] = (
        "Self-report only: a token was obtained and delivered. This process verified "
        "nothing; whether it attests anything is decided by the relying party, the "
        "MCPForge API. No repository job was run."
    )
    return EXIT_OK, record


def _use_vendored_packages() -> None:
    """Import the payload from the image itself, whatever the environment says.

    The image's `ENV PYTHONPATH` names the same directory. This removes the
    dependency on that `ENV` reaching the process, for the reason given at
    `WORKSPACE_ROOT`. The directory is inside the attested image, so this adds
    nothing to what the digest covers.
    """

    sys.dont_write_bytecode = True
    vendored = WORKLOAD_ROOT / "site-packages"
    if vendored.is_dir() and str(vendored) not in sys.path:
        sys.path.insert(0, str(vendored))


def _route_logs_to_stderr() -> None:
    """Keep stdout to the one JSON line. Redaction stays in the chain."""

    try:
        import structlog

        from mcpforge.logging import redact_processor
    except ImportError:
        return
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def main(argv: list[str] | None = None) -> int:
    del argv
    _use_vendored_packages()
    _route_logs_to_stderr()
    code, record = execute(dict(os.environ))
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
