"""MCPForge Confidential Space workload entrypoint — F8-02a.

This is the process that Confidential Space launches inside the attested VM. It
is deliberately small, because everything in this image sits **inside** the
trust boundary: the image digest is what `AttestationPolicy.image_digest` pins,
so any file baked in here is a file the attestation vouches for.

What it does today, and nothing more:

1. Reads its configuration from the environment and **refuses to start** if any
   required value is absent. There is no default for any of them. A workload
   that starts with a guessed audience would obtain an attestation token bound
   to the wrong nonce, and a token bound to the wrong nonce is a replayable
   token.
2. Refuses to run as root.
3. Refuses to run if credential-shaped material is reachable — an ADC file, a
   `.env`, a private key. `03_SECURITY_ACCESS.md` §9: key-file ADC is not a
   supported configuration, so `GOOGLE_APPLICATION_CREDENTIALS` being set at all
   is a refusal rather than a fallback.
4. Confirms the secure-executor payload the image claims to carry actually
   imports.
5. Prints one line of JSON describing what it found and exits.

What it does **not** do, stated here so no reader has to infer it:

- It does not obtain an attestation token. Nothing in MCPForge does yet; that is
  `F8-02`, which is `BLOCKED` on blocker B-04 and is not simulated.
- It does not report a `TrustLevel`. Trust is produced by exactly one function,
  `verify_attestation_token` in `mcpforge.execution.attestation`, and this
  process has verified nothing, so it has nothing to report. It emits
  `attestation_token_obtained: false` instead of a level.
- It does not run a repository job. There is no job runner in this image. The
  JSON says `job_runner_present: false`.

Every refusal is a distinct exit code so that an operator reading only the exit
status still knows which control fired.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

#: Exit codes. 0 means every precondition held. Everything else is a refusal,
#: never a degraded start.
EXIT_OK = 0
EXIT_CONFIG_MISSING = 10
EXIT_CONFIG_INVALID = 11
EXIT_RUNNING_AS_ROOT = 12
EXIT_CREDENTIAL_PRESENT = 13
EXIT_WORKSPACE_UNUSABLE = 14
EXIT_WORKLOAD_PAYLOAD_MISSING = 15

#: Configuration this workload requires. Absent or blank is a refusal. None of
#: these has a fallback value anywhere in this file — grep for the names and the
#: only occurrences are here and in the refusal path.
REQUIRED_ENVIRONMENT: tuple[str, ...] = (
    # Correlates this run with the orchestrator's run. Becomes a directory name,
    # so its shape is constrained.
    "MCPFORGE_RUN_ID",
    # The per-run nonce that the attestation token must carry as its single
    # audience. `attestation.py` refuses a token whose audience is not exactly
    # this value; a default here would defeat that.
    "MCPFORGE_ATTESTATION_AUDIENCE",
    # The path jail root. Baked in by the Dockerfile and deliberately absent
    # from `tee.launch_policy.allow_env_override`, so an operator cannot move
    # the jail — but it is still validated rather than assumed.
    "MCPFORGE_WORKSPACE_ROOT",
)

#: A run id becomes a filesystem path component in the executor, so it is
#: constrained rather than trusted. No separators, no leading dot, bounded.
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

    def __init__(self, code: int, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.code = code
        self.reason = reason
        self.detail = detail


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

    The class is imported, never constructed: constructing it probes the kernel
    for unprivileged namespaces, which is a runtime decision and not a
    precondition check.
    """

    try:
        from mcpforge.execution.development import DevelopmentSecureExecutor
    except ImportError as exc:
        raise RefusalError(
            EXIT_WORKLOAD_PAYLOAD_MISSING,
            "WORKLOAD_PAYLOAD_MISSING",
            f"the secure executor payload did not import: {exc}",
        ) from exc

    return f"{DevelopmentSecureExecutor.__module__}.{DevelopmentSecureExecutor.__qualname__}"


def preflight(environment: dict[str, str]) -> dict[str, object]:
    """Run every precondition in order and return the readiness record.

    Raises `RefusalError` on the first failure. Order matters only in that
    configuration is read first, so an operator who supplied nothing is told
    that rather than being told about a workspace they never configured.
    """

    config = read_configuration(environment)
    check_not_root()
    workspace_root = Path(config["MCPFORGE_WORKSPACE_ROOT"])
    check_no_credentials((WORKLOAD_ROOT, Path.home()), environment)
    check_workspace(workspace_root)
    workload = check_workload_payload()

    return {
        "status": "PREFLIGHT_OK",
        "ok": True,
        "run_id": config["MCPFORGE_RUN_ID"],
        "workspace_root": str(workspace_root),
        "workload": workload,
        "uid": os.geteuid(),
        "python": sys.version.split()[0],
        # Deliberately not a TrustLevel. This process has verified no
        # attestation, so it has no trust level to report.
        "attestation_token_obtained": False,
        "job_runner_present": False,
        "note": (
            "Preflight only. This image obtains no attestation token and runs no "
            "repository job; both are F8-02, which is BLOCKED on blocker B-04 and "
            "is not simulated."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        record = preflight(dict(os.environ))
    except RefusalError as refusal:
        sys.stdout.write(
            json.dumps(
                {
                    "status": "REFUSED",
                    "ok": False,
                    "reason": refusal.reason,
                    "detail": refusal.detail,
                    "exit_code": refusal.code,
                },
                sort_keys=True,
            )
            + "\n"
        )
        sys.stdout.flush()
        return refusal.code

    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
