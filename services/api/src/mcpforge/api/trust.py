"""Trust panel state — F8-03, `04_FRONTEND_SPEC.md` §8.

The panel exists to show the developer the real security state of their session,
so every field here is read from something that already decides the behaviour:

- the boundary and access mode come from the stored `Project`, the same record
  `github/boundary.py` enforces against;
- the quarantine list comes from the persisted analysis artifact's
  `quarantined_paths`, which is the filtering pipeline's own output — paths and
  nothing else (`security/pipeline.py` never puts contents in that field);
- the branch-protection strings are the live constants from
  `github/branches.py`, not a copy of them, so the panel cannot describe a rule
  the writer no longer applies;
- the execution row is the running provider's own `trust_level` and
  `attestation()`, passed through `AttestationOutcome`, which is the single
  place the "no evidence means no raised trust level" invariant lives.

**Two things this module deliberately does not do.**

1. It never names the attested trust level. There is no comparison against it,
   no boolean derived from it, and no branch on it — the enum value is passed
   through to the client and the UI does the one comparison it is allowed to do
   (`apps/web/src/components/trust/secure-execution-row.tsx`). That keeps this
   module inside the F8-01 rule swept by
   `test_exactly_one_function_can_produce_the_attested_trust_level`.
2. It reports no WebMCP state. Whether the browser exposes
   `document.modelContext` is a fact about the browser, not about the server; a
   server-supplied answer would be a guess. The panel's adapter row is fed by
   the real adapter in the page.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from mcpforge.api.deps import CurrentIdentity
from mcpforge.config import SecureExecutorKind, Settings
from mcpforge.execution.attestation import (
    AttestationEvidence,
    AttestationFailure,
    AttestationOutcome,
    TrustLevel,
)
from mcpforge.execution.provider import SecureExecutionProvider
from mcpforge.github.branches import BRANCH_PREFIX, BRANCH_SHAPE, PROTECTED_NAMES
from mcpforge.models.core import AccessMode, ArtifactKind, Project, Session
from mcpforge.security import filters
from mcpforge.store.port import NotFoundError, Store

router = APIRouter(prefix="/api", tags=["trust"])

#: The field on the analysis payload holding quarantined paths. It is the field
#: name on `RepositoryIndex` itself, so this reader and the pipeline that writes
#: it cannot drift apart without
#: `test_quarantine_field_is_the_repository_index_field` failing.
QUARANTINE_FIELD: Final = "quarantined_paths"


class RepositoryBoundaryDto(BaseModel):
    """What this project is allowed to touch. Never a repository list."""

    bound: bool
    repository_full_name: str | None
    base_branch: str | None
    is_demo: bool


class SecretFilteringDto(BaseModel):
    """Filtering state, and the quarantine record if analysis has run.

    `quarantined_count` is `None` — not `0` — when nothing has been analyzed
    yet. Zero would read as "your repository was scanned and is clean", which is
    a claim about a scan that never happened.
    """

    active: bool
    rule_count: int
    analyzed: bool
    quarantined_count: int | None
    #: Paths only. `03_SECURITY_ACCESS.md` §4: a quarantined file is never
    #: opened, so no content exists to leak here even by mistake.
    quarantined_paths: list[str]


class AttestationEvidenceDto(BaseModel):
    """Verified facts, if any exist. Absent is the normal state today."""

    issuer: str
    audience: str
    subject: str
    image_digest: str
    image_reference: str
    workload_service_account: str
    hardware_model: str
    software_name: str
    debug_status: str
    issued_at: str
    expires_at: str
    verified_at: str


class SecureExecutionDto(BaseModel):
    """The execution boundary, as an enum plus what is behind it.

    There is deliberately no `hardware_attested` boolean. `TrustLevel` is an
    enum precisely so that a flattening to `true`/`false` cannot happen on the
    way to the screen (`02_ARCHITECTURE.md` §8, Context State Log 0001).
    """

    trust_level: TrustLevel
    configured_executor: SecureExecutorKind
    #: False when this API process holds no execution provider — which is the
    #: current state, since no route runs a job yet. Said plainly rather than
    #: implied by an unattested trust level.
    provider_running: bool
    evidence: AttestationEvidenceDto | None
    #: Why the trust level is what it is. Always populated.
    detail: str


class BranchProtectionDto(BaseModel):
    """The writer's live rules, read from `github/branches.py`."""

    branch_prefix: str
    branch_shape: str
    protected_names: list[str]


class TrustStateDto(BaseModel):
    session_id: str
    project_id: str
    repository: RepositoryBoundaryDto
    access_mode: AccessMode
    secret_filtering: SecretFilteringDto
    secure_execution: SecureExecutionDto
    branch_protection: BranchProtectionDto


def _store(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


def _executor(request: Request) -> SecureExecutionProvider | None:
    provider: SecureExecutionProvider | None = getattr(request.app.state, "executor", None)
    return provider


@router.get("/sessions/{session_id}/trust", response_model=TrustStateDto)
async def get_trust_state(
    session_id: str, identity: CurrentIdentity, request: Request
) -> TrustStateDto:
    """Everything the trust panel renders, for one session.

    Session-scoped because the quarantine record is: it belongs to the analysis
    run, not to the account.
    """
    store = _store(request)
    try:
        session: Session = await store.get_session(session_id, identity.subject)
        project: Project = await store.get_project(session.project_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from exc

    analysis = await store.get_artifact(session.id, ArtifactKind.ANALYSIS, identity.subject)
    settings: Settings = request.app.state.settings

    return TrustStateDto(
        session_id=session.id,
        project_id=project.id,
        repository=RepositoryBoundaryDto(
            bound=project.repository_full_name is not None,
            repository_full_name=project.repository_full_name,
            base_branch=project.base_branch,
            is_demo=project.is_demo,
        ),
        access_mode=project.access_mode,
        secret_filtering=secret_filtering_state(analysis.payload if analysis else None),
        secure_execution=await secure_execution_state(
            _executor(request), configured=settings.secure_executor
        ),
        branch_protection=BranchProtectionDto(
            branch_prefix=BRANCH_PREFIX,
            branch_shape=BRANCH_SHAPE.pattern,
            protected_names=sorted(PROTECTED_NAMES),
        ),
    )


def secret_filtering_state(analysis_payload: dict[str, Any] | None) -> SecretFilteringDto:
    """Filtering state from the rules that are actually loaded, and the real record.

    `active` counts the rule sets `security/filters.py` matches against rather
    than returning a literal `True`: emptying them makes this row say the
    filtering is inactive, which is what
    `test_filtering_reports_inactive_when_the_rules_are_empty` checks.
    """
    rule_count = (
        len(filters.SECRET_PATH_PATTERNS)
        + len(filters.SECRET_DIR_SEGMENTS)
        + len(filters.CONTENT_PATTERNS)
    )
    if analysis_payload is None:
        return SecretFilteringDto(
            active=rule_count > 0,
            rule_count=rule_count,
            analyzed=False,
            quarantined_count=None,
            quarantined_paths=[],
        )

    paths = quarantined_paths_of(analysis_payload)
    return SecretFilteringDto(
        active=rule_count > 0,
        rule_count=rule_count,
        analyzed=True,
        quarantined_count=len(paths),
        quarantined_paths=paths,
    )


def quarantined_paths_of(payload: dict[str, Any]) -> list[str]:
    """The quarantined paths on an analysis payload, and nothing else from it.

    One field is read by name and every entry must be a string. A payload that
    carried file contents — which it must not, `Artifact` forbids it — still
    could not put them here, because nothing but that one list is copied out.
    """
    raw = payload.get(QUARANTINE_FIELD)
    if not isinstance(raw, list):
        return []
    return sorted(entry for entry in raw if isinstance(entry, str) and entry)


async def secure_execution_state(
    provider: SecureExecutionProvider | None, *, configured: SecureExecutorKind
) -> SecureExecutionDto:
    """The execution row, from the provider itself.

    With no provider in this process there is nothing to report but the
    configured intent, and `AttestationOutcome.rejected` supplies the trust
    level — the same call every failed verification makes. With a provider, its
    level is passed to `AttestationOutcome` together with whatever
    `attestation()` returned, so the invariant that evidence is what raises a
    trust level is enforced by the one implementation of it in
    `execution/attestation.py` rather than restated here.
    """
    if provider is None:
        outcome = AttestationOutcome.rejected(
            AttestationFailure.NO_TOKEN,
            "No execution provider is running in this API process, so no attestation exists.",
        )
        return _execution_dto(outcome, configured=configured, provider_running=False)

    evidence = await provider.attestation()
    if evidence is None:
        outcome = AttestationOutcome.rejected(
            AttestationFailure.NO_TOKEN,
            "The execution provider produced no attestation evidence.",
        )
    else:
        try:
            outcome = AttestationOutcome(trust_level=provider.trust_level, evidence=evidence)
        except ValueError as exc:  # pragma: no cover - the constructor's own guard
            outcome = AttestationOutcome.rejected(
                AttestationFailure.VERIFICATION_ERROR, f"Unusable attestation result: {exc}"
            )
    return _execution_dto(outcome, configured=configured, provider_running=True)


def _execution_dto(
    outcome: AttestationOutcome, *, configured: SecureExecutorKind, provider_running: bool
) -> SecureExecutionDto:
    return SecureExecutionDto(
        trust_level=outcome.trust_level,
        configured_executor=configured,
        provider_running=provider_running,
        evidence=_evidence_dto(outcome.evidence),
        detail=_detail_of(outcome),
    )


def _detail_of(outcome: AttestationOutcome) -> str:
    """A reason for every state, including the one that should not occur.

    A provider that hands back evidence without raising its own trust level is
    not upgraded here — it is reported as it is, with the discrepancy named.
    """
    if outcome.detail:
        return outcome.detail
    if outcome.trust_level is TrustLevel.DEVELOPMENT_ISOLATION:
        return "The provider supplied evidence but did not raise its trust level."
    return "Execution is backed by verified attestation evidence."


def _evidence_dto(evidence: AttestationEvidence | None) -> AttestationEvidenceDto | None:
    if evidence is None:
        return None
    return AttestationEvidenceDto(
        issuer=evidence.issuer,
        audience=evidence.audience,
        subject=evidence.subject,
        image_digest=evidence.image_digest,
        image_reference=evidence.image_reference,
        workload_service_account=evidence.workload_service_account,
        hardware_model=evidence.hardware_model,
        software_name=evidence.software_name,
        debug_status=evidence.debug_status,
        issued_at=evidence.issued_at.isoformat(),
        expires_at=evidence.expires_at.isoformat(),
        verified_at=evidence.verified_at.isoformat(),
    )
