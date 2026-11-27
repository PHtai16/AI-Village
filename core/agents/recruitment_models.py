"""Domain contracts, status transitions, and persistence port for P05.07 Agent Recruitment."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Annotated, Any, Literal, Protocol, runtime_checkable
import uuid

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from core.agents.manifest import AgentManifest
from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    CapabilityId,
    EventId,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    reject_secret_text,
    validate_aware_datetime,
)
from core.governance.models import TrustedAuthContext
from core.tasks.transitions import ActorTrustKind
from engines.antigravity.diagnostics import redact_secrets

_RECRUITMENT_ID_RE = re.compile(r"^recr_[a-z0-9][a-z0-9_-]{3,63}$")

RecruitmentStatus = Literal[
    "PROPOSED",
    "EVALUATED_PASS",
    "EVALUATED_FAIL",
    "ACTIVATED",
    "REJECTED",
]

VALID_RECRUITMENT_TRANSITIONS: dict[RecruitmentStatus, frozenset[RecruitmentStatus]] = {
    "PROPOSED": frozenset({"EVALUATED_PASS", "EVALUATED_FAIL", "REJECTED"}),
    "EVALUATED_FAIL": frozenset({"EVALUATED_PASS", "EVALUATED_FAIL", "REJECTED"}),
    "EVALUATED_PASS": frozenset({"ACTIVATED", "REJECTED"}),
    "ACTIVATED": frozenset(),
    "REJECTED": frozenset(),
}

TERMINAL_RECRUITMENT_STATUSES: frozenset[RecruitmentStatus] = frozenset(
    {"ACTIVATED", "REJECTED"}
)


def new_recruitment_id() -> str:
    """Generate a unique canonical Recruitment Proposal identifier (`recr_*`)."""
    return f"recr_{uuid.uuid4().hex[:16]}"


def validate_recruitment_id(value: str) -> str:
    """Validate canonical `recr_*` recruitment identifier syntax without secrets."""
    cleaned = ensure_not_secret(value, "proposal_id")
    if not _RECRUITMENT_ID_RE.fullmatch(cleaned):
        raise ValueError(
            f"proposal_id must match '^recr_[a-z0-9][a-z0-9_-]{{3,63}}$', got {value!r}."
        )
    return cleaned


RecruitmentId = Annotated[str, AfterValidator(validate_recruitment_id)]


class RecruitmentError(RuntimeError):
    """Base fail-closed exception raised by Agent Recruitment operations."""

    def __init__(self, error_code: str, message: str) -> None:
        cleaned = redact_secrets(message)
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned


class RecruitmentValidationError(RecruitmentError):
    """Raised when candidate manifest, role, or request fields fail validation."""


class RecruitmentEvaluationError(RecruitmentError):
    """Raised when evaluation rules or evaluator independence fail."""


class RecruitmentGovernanceError(RecruitmentError):
    """Raised when Governance policy or Approval Gate denies a recruitment action."""


class RecruitmentStateError(RecruitmentError):
    """Raised on invalid recruitment lifecycle transitions or duplicate proposals."""


class RecruitmentNotFoundError(RecruitmentError):
    """Raised when querying or mutating a nonexistent recruitment proposal."""


class StaleRecruitmentVersionError(RecruitmentError):
    """Raised on optimistic lock conflict updating a recruitment proposal."""


class RecruitmentEvaluationResult(BaseModel):
    """Immutable evaluation outcome attached to a Recruitment Proposal."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    summary: str = Field(min_length=3, max_length=1000)
    checks_passed: list[str] = Field(default_factory=list, max_length=64)
    findings: list[str] = Field(default_factory=list, max_length=64)
    evaluator_id: str = Field(min_length=1, max_length=128)
    evaluator_kind: ActorTrustKind
    manifest_sha256: Sha256Hex
    evaluated_at: datetime

    @field_validator("summary", "evaluator_id")
    @classmethod
    def _validate_text(cls, value: str, info: object) -> str:
        """Ensure evaluation labels remain non-blank and secret-free."""
        return ensure_not_secret(value, getattr(info, "field_name", "evaluation_field"))

    @field_validator("checks_passed", "findings")
    @classmethod
    def _validate_lists(cls, values: list[str], info: object) -> list[str]:
        """Ensure check and finding entries stay secret-free."""
        field_name = getattr(info, "field_name", "evaluation_list")
        return [ensure_not_secret(item, field_name) for item in values]

    @field_validator("evaluated_at")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware evaluation timestamp."""
        return validate_aware_datetime(value, "evaluated_at")


class RecruitmentProposalRecord(BaseModel):
    """Canonical candidate Agent recruitment proposal covering role, tools, permissions, and evaluation."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: RecruitmentId = Field(default_factory=new_recruitment_id)
    candidate_agent_id: AgentId
    candidate_manifest: AgentManifest
    manifest_sha256: Sha256Hex
    rationale: str = Field(min_length=3, max_length=1000)
    needed_capabilities: list[CapabilityId] = Field(min_length=1, max_length=32)
    task_id: TaskId | None = None
    proposed_by: str = Field(min_length=1, max_length=128)
    proposed_by_kind: ActorTrustKind
    status: RecruitmentStatus = "PROPOSED"
    evaluation: RecruitmentEvaluationResult | None = None
    activated_manifest_revision: int | None = Field(default=None, ge=1)
    rejection_reason: str | None = Field(default=None, max_length=500)
    correlation_id: str = Field(min_length=1, max_length=80)
    created_at: datetime
    evaluated_at: datetime | None = None
    activated_at: datetime | None = None
    rejected_at: datetime | None = None

    @field_validator("rationale", "proposed_by", "correlation_id")
    @classmethod
    def _validate_labels(cls, value: str, info: object) -> str:
        """Reject secret patterns or blank strings in metadata."""
        return ensure_not_secret(value, getattr(info, "field_name", "proposal_field"))

    @field_validator("rejection_reason")
    @classmethod
    def _validate_rejection(cls, value: str | None) -> str | None:
        """Reject secret patterns in optional rejection rationale."""
        if value is None:
            return None
        cleaned = value.strip()
        reject_secret_text(cleaned, "rejection_reason")
        return cleaned or None

    @field_validator("created_at", "evaluated_at", "activated_at", "rejected_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        """Require timezone-aware timestamps when set."""
        if value is None:
            return None
        return validate_aware_datetime(value, getattr(info, "field_name", "proposal_ts"))

    def compute_content_sha256(self) -> str:
        """Compute canonical SHA-256 over immutable candidate proposal payload."""
        canonical = {
            "candidate_agent_id": self.candidate_agent_id,
            "manifest_sha256": self.manifest_sha256,
            "rationale": self.rationale,
            "needed_capabilities": list(self.needed_capabilities),
            "task_id": self.task_id,
            "proposed_by": self.proposed_by,
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class StoredRecruitmentProposal(BaseModel):
    """Persisted RecruitmentProposalRecord paired with optimistic locking version."""

    model_config = ConfigDict(extra="forbid")

    proposal: RecruitmentProposalRecord
    record_version: int = Field(ge=1)
    last_audit_event_id: EventId | None = None


class RecruitmentRevisionRecord(BaseModel):
    """Immutable audit revision row for one recruitment lifecycle transition."""

    model_config = ConfigDict(extra="forbid")

    revision_id: str = Field(
        default_factory=lambda: f"recr_rev_{uuid.uuid4().hex[:16]}",
        min_length=8,
        max_length=64,
    )
    proposal_id: RecruitmentId
    candidate_agent_id: AgentId
    record_version: int = Field(ge=1)
    status: RecruitmentStatus
    actor_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)
    manifest_sha256: Sha256Hex
    event_id: EventId
    timestamp: datetime

    @field_validator("actor_id", "reason")
    @classmethod
    def _validate_text(cls, value: str, info: object) -> str:
        """Ensure revision actor and reason remain clean and secret-free."""
        return ensure_not_secret(value, getattr(info, "field_name", "revision_field"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware revision timestamp."""
        return validate_aware_datetime(value, "timestamp")


class RecruitmentProposalRequest(BaseModel):
    """Request to propose a new candidate Worker Agent."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: RecruitmentId | None = None
    candidate_manifest: AgentManifest | dict[str, Any]
    rationale: str = Field(min_length=3, max_length=1000)
    needed_capabilities: list[CapabilityId] = Field(min_length=1, max_length=32)
    task_id: TaskId | None = None
    proposer_id: str = Field(min_length=1, max_length=128)
    proposer_kind: ActorTrustKind = "orchestrator"
    auth_context: TrustedAuthContext | None = None
    timestamp: datetime
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("rationale", "proposer_id", "correlation_id")
    @classmethod
    def _validate_text(cls, value: str | None, info: object) -> str | None:
        """Reject secret patterns in proposal request fields."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "proposal_request"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require timezone-aware timestamp."""
        return validate_aware_datetime(value, "timestamp")


class RecruitmentEvaluationRequest(BaseModel):
    """Request to evaluate a proposed candidate before activation."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: RecruitmentId
    evaluator_id: str = Field(min_length=1, max_length=128)
    evaluator_kind: ActorTrustKind = "qa_verifier"
    auth_context: TrustedAuthContext | None = None
    summary: str = Field(min_length=3, max_length=1000)
    checks_passed: list[str] = Field(default_factory=list, max_length=64)
    findings: list[str] = Field(default_factory=list, max_length=64)
    workspace_root: str | None = Field(default=None, min_length=1, max_length=512)
    timestamp: datetime
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("evaluator_id", "summary", "workspace_root", "correlation_id")
    @classmethod
    def _validate_text(cls, value: str | None, info: object) -> str | None:
        """Reject secret patterns in evaluation request fields."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "eval_request"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require timezone-aware timestamp."""
        return validate_aware_datetime(value, "timestamp")


class RecruitmentActivationRequest(BaseModel):
    """Request to activate an EVALUATED_PASS candidate into AgentRegistryService."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: RecruitmentId
    actor_id: str = Field(min_length=1, max_length=128)
    actor_kind: ActorTrustKind = "operator"
    auth_context: TrustedAuthContext | None = None
    validated_capabilities: list[CapabilityId] = Field(default_factory=list, max_length=32)
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)
    timestamp: datetime
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("actor_id", "approval_id", "correlation_id")
    @classmethod
    def _validate_text(cls, value: str | None, info: object) -> str | None:
        """Reject secret patterns in activation request fields."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "activation_request"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require timezone-aware timestamp."""
        return validate_aware_datetime(value, "timestamp")


class RecruitmentRejectRequest(BaseModel):
    """Request to reject a recruitment proposal with an audited rationale."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: RecruitmentId
    actor_id: str = Field(min_length=1, max_length=128)
    actor_kind: ActorTrustKind = "operator"
    auth_context: TrustedAuthContext | None = None
    reason: str = Field(min_length=3, max_length=500)
    timestamp: datetime
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("actor_id", "reason", "correlation_id")
    @classmethod
    def _validate_text(cls, value: str | None, info: object) -> str | None:
        """Reject secret patterns in rejection request fields."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "reject_request"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require timezone-aware timestamp."""
        return validate_aware_datetime(value, "timestamp")


@runtime_checkable
class RecruitmentRepository(Protocol):
    """Persistence port isolating ``AgentRecruitmentService`` from SQLite internals."""

    def create_proposal_atomic(
        self,
        *,
        proposal: RecruitmentProposalRecord,
        revision: RecruitmentRevisionRecord,
        event: Event,
    ) -> StoredRecruitmentProposal:
        """Atomically persist Event, recruitment proposal, and immutable revision v1."""

    def transition_proposal_atomic(
        self,
        *,
        proposal: RecruitmentProposalRecord,
        expected_record_version: int,
        revision: RecruitmentRevisionRecord,
        event: Event,
    ) -> StoredRecruitmentProposal:
        """Atomically transition proposal under optimistic lock and append revision + Event."""

    def get_proposal(self, proposal_id: str) -> StoredRecruitmentProposal | None:
        """Load persisted recruitment proposal by identifier or return None."""

    def list_proposals(
        self,
        *,
        candidate_agent_id: str | None = None,
        status: RecruitmentStatus | None = None,
        task_id: str | None = None,
    ) -> list[StoredRecruitmentProposal]:
        """List persisted recruitment proposals filtered by candidate, status, or task."""

    def list_revisions(self, proposal_id: str) -> list[RecruitmentRevisionRecord]:
        """Return ascending immutable revision history for ``proposal_id``."""
