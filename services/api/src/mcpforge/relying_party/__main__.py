"""`python -m mcpforge.relying_party` — issue, show and verify attestation runs.

    begin            issue a run; print MCPFORGE_RUN_ID=… and MCPFORGE_ATTESTATION_AUDIENCE=…
    show RUN_ID      print the same two lines for a pending, in-date run; refuse otherwise
    verify RUN_ID    fetch the run's delivered token and verify it; exit 0 only if verified

`launch.sh` calls `show` and has no other source for the audience, so a VM can
be launched only with a nonce this relying party issued. `verify` runs the same
code as the API route; the run is single-use across both, because they share
the run store. A running API server's trust panel reflects only verifications
that server performed (`POST /api/attestation-runs/{run_id}/verify`).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from mcpforge.config import ConfigError, get_settings
from mcpforge.execution.confidential_space import RUN_TTL
from mcpforge.relying_party import build_executor
from mcpforge.relying_party.runs import FileAttestationRunStore

EXIT_REFUSED = 3


def _store(directory: str | None) -> FileAttestationRunStore:
    if directory is not None:
        return FileAttestationRunStore(Path(directory))
    return FileAttestationRunStore(get_settings().confidential_space_run_directory)


def _print_run(run_id: str, audience: str) -> None:
    sys.stdout.write(f"MCPFORGE_RUN_ID={run_id}\nMCPFORGE_ATTESTATION_AUDIENCE={audience}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mcpforge.relying_party")
    parser.add_argument("--store", help="run store directory (default: from settings)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("begin")
    show = commands.add_parser("show")
    show.add_argument("run_id")
    verify = commands.add_parser("verify")
    verify.add_argument("run_id")
    arguments = parser.parse_args(argv)

    store = _store(arguments.store)
    if arguments.command == "begin":
        run = store.issue()
        _print_run(run.run_id, run.audience)
        sys.stderr.write(
            f"Issued {run.run_id}. Launch with: infra/confidential-space/launch.sh "
            f"--run-id {run.run_id}\n"
        )
        return 0

    if arguments.command == "show":
        record = store.lookup(arguments.run_id)
        if record is None:
            sys.stderr.write("This relying party issued no such run.\n")
            return EXIT_REFUSED
        if record.consumed:
            sys.stderr.write("This run has already been verified; issue a new one.\n")
            return EXIT_REFUSED
        if datetime.now(UTC) - record.run.issued_at > RUN_TTL:
            sys.stderr.write("This run has expired; issue a new one.\n")
            return EXIT_REFUSED
        _print_run(record.run.run_id, record.run.audience)
        return 0

    settings = get_settings()
    if arguments.store is not None:
        settings = settings.model_copy(update={"confidential_space_run_dir": arguments.store})
    try:
        executor = build_executor(settings)
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_REFUSED
    result = executor.verify_run_blocking(arguments.run_id)
    sys.stdout.write(
        json.dumps(
            {
                "run_id": result.run_id,
                "verified": result.outcome.verified,
                "failure": result.failure.value if result.failure is not None else None,
                "attestation_failure": (
                    result.outcome.failure.value if result.outcome.failure is not None else None
                ),
                "detail": result.outcome.detail,
                "trust_level": result.outcome.trust_level.value,
                "token_sha256_prefix": result.token.sha256_prefix if result.token else None,
                "consumed": result.consumed,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if result.outcome.verified else EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
