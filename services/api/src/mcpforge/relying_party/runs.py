"""The relying party's record of issued attestation runs — F8-02.

The API service issues each run: a run id and a fresh audience that the
attestation token must carry. The audience is the nonce, and a nonce protects
against replay only if the **verifier** chose it — so it is generated here and
nowhere else. `launch.sh` reads it from this record; it cannot supply its own.

A run is single-use. `consume` renames `<run_id>.pending.json` to
`<run_id>.consumed.json`, and a rename is atomic on POSIX: of two concurrent
verifiers exactly one succeeds, and the other is told the run is consumed.
The directory is created `0700` and every record `0600`.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mcpforge.execution.confidential_space import (
    RUN_ID_SHAPE,
    AttestationRun,
    RunRecord,
    check_launcher_audience,
)

AUDIENCE_PREFIX = "mcpforge-attestation-"
_PENDING = ".pending.json"
_CONSUMED = ".consumed.json"


class FileAttestationRunStore:
    """Issued runs as files. Implements `AttestationRunStore`."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory

    def issue(self, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> AttestationRun:
        """Mint a run id and a 128-bit random audience, and record them pending."""
        now = clock()
        run = AttestationRun(
            run_id=f"cs-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}",
            audience=AUDIENCE_PREFIX + secrets.token_hex(16),
            issued_at=now,
        )
        check_launcher_audience(run.audience)
        assert RUN_ID_SHAPE.fullmatch(run.run_id)
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        record = json.dumps(
            {
                "run_id": run.run_id,
                "audience": run.audience,
                "issued_at": run.issued_at.isoformat(),
            }
        ).encode("utf-8")
        descriptor = os.open(
            self._path(run.run_id, _PENDING), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(record)
        return run

    def lookup(self, run_id: str) -> RunRecord | None:
        if not RUN_ID_SHAPE.fullmatch(run_id):
            return None
        # Read, don't check-then-read: a concurrent `consume` can rename the
        # pending record between an existence check and the read. Found by
        # `test_two_concurrent_verifiers_cannot_both_consume_a_run`.
        for suffix, consumed in ((_PENDING, False), (_CONSUMED, True)):
            try:
                run = self._read(self._path(run_id, suffix), run_id)
            except FileNotFoundError:
                continue
            except (ValueError, KeyError, TypeError, AttributeError):
                # A record this store cannot read is not a run it can vouch
                # for: refused as unknown, never an exception on the verify path.
                return None
            return RunRecord(run=run, consumed=consumed)
        return None

    def consume(self, run_id: str) -> bool:
        if not RUN_ID_SHAPE.fullmatch(run_id):
            return False
        try:
            os.rename(self._path(run_id, _PENDING), self._path(run_id, _CONSUMED))
        except FileNotFoundError:
            return False
        return True

    def _path(self, run_id: str, suffix: str) -> Path:
        return self._directory / f"{run_id}{suffix}"

    @staticmethod
    def _read(path: Path, run_id: str) -> AttestationRun:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("run_id") != run_id:
            raise ValueError(f"{path.name} does not describe run {run_id}")
        return AttestationRun(
            run_id=run_id,
            audience=str(document["audience"]),
            issued_at=datetime.fromisoformat(document["issued_at"]),
        )
