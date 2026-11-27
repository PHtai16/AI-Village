"""Pure domain contracts for Phase 06 Meetings, Structured Debate, Decisions, Conflicts, and Delivery."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Annotated, Any, Literal, Protocol, runtime_checkable
import uuid

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.artifact import ArtifactVerificationStatus
from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    ArtifactId,
    EventId,
    RunId,
    SessionId,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    validate_agent_id,
    validate_aware_datetime,
    validate_safe_relative_path,
)
from core.governance.models import (
    TrustedAuthContext,
    actor_kind_to_event_source,
    resolve_trusted_auth_context,
)
from core.orchestration.handoff_models import HandoffId, HandoffRevisionRecord, StoredWorkerHandoff
from core.orchestration.planning_models import PlanId, StepId
from core.orchestration.team_models import TeamProposalId
from core.tasks.transitions import ActorTrustKind
from engines.antigravity.diagnostics import redact_secrets

_MTG_ID_RE = re.compile(r"^mtg_[a-z0-9][a-z0-9_-]{3,63}$")
_DEC_ID_RE = re.compile(r"^dec_[a-z0-9][a-z0-9_-]{3,63}$")
_CFL_ID_RE = re.compile(r"^cfl_[a-z0-9][a-z0-9_-]{3,63}$")
_DLV_ID_RE = re.compile(r"^dlv_[a-z0-9][a-z0-9_-]{3,63}$")

MeetingStatus = Literal["OPEN", "NEEDS_MASTER_DECISION", "DECIDED", "CLOSED"]
MeetingTurnKind = Literal["PROPOSAL", "OBJECTION", "EVIDENCE", "UNCERTAINTY", "ACTION_ITEM"]
ConflictType = Literal["FILE_WRITE_CONTENTION", "STEP_OUTPUT_CONFLICT", "ARTIFACT_COLLISION"]
ConflictStatus = Literal["OPEN", "RESOLVED_SERIALIZED", "RESOLVED_DECIDED"]
DeliveryVerificationStatus = Literal["VERIFIED", "NOT_VERIFIED"]

VALID_MEETING_TRANSITIONS: dict[MeetingStatus, frozenset[MeetingStatus]] = {
    "OPEN": frozenset({"NEEDS_MASTER_DECISION", "DECIDED", "CLOSED"}),
    "NEEDS_MASTER_DECISION": frozenset({"DECIDED", "CLOSED"}),
    "DECIDED": frozenset({"CLOSED"}),
    "CLOSED": frozenset(),
}


def _new_prefixed_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _validate_id(value: str, regex: re.Pattern[str], field: str) -> str:
    cleaned = ensure_not_secret(value, field)
    if not regex.fullmatch(cleaned):
        raise ValueError(f"{field} has invalid format: {value!r}.")
    return cleaned


MeetingId = Annotated[str, AfterValidator(lambda v: _validate_id(v, _MTG_ID_RE, "meeting_id"))]
DecisionId = Annotated[str, AfterValidator(lambda v: _validate_id(v, _DEC_ID_RE, "decision_id"))]
ConflictId = Annotated[str, AfterValidator(lambda v: _validate_id(v, _CFL_ID_RE, "conflict_id"))]
DeliveryId = Annotated[str, AfterValidator(lambda v: _validate_id(v, _DLV_ID_RE, "delivery_id"))]


def new_meeting_id() -> str:
    """Generate unique Meeting identifier (`mtg_*`)."""
    return _new_prefixed_id("mtg")


def new_decision_id() -> str:
    """Generate unique Master Decision identifier (`dec_*`)."""
    return _new_prefixed_id("dec")


def new_conflict_id() -> str:
    """Generate unique Collaboration Conflict identifier (`cfl_*`)."""
    return _new_prefixed_id("cfl")


def new_delivery_id() -> str:
    """Generate unique Master Delivery Summary identifier (`dlv_*`)."""
    return _new_prefixed_id("dlv")


class CollaborationError(RuntimeError):
    """Base fail-closed error for Phase 06 meetings, decisions, conflicts, and delivery."""

    def __init__(self, error_code: str, message: str) -> None:
        cleaned = redact_secrets(message)
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned


class MeetingPermissionError(CollaborationError):
    """Raised when Governance denies convening, participating, deciding, or delivering."""


class MeetingStateError(CollaborationError):
    """Raised on invalid meeting transitions, missing participants, or closed state."""


class MeetingTurnLimitExceededError(CollaborationError):
    """Raised when rounds or turn count hit configured limits to prevent infinite loops."""


class FakeConsensusRejectedError(CollaborationError):
    """Raised on echo-chamber or unresolved-objection fake consensus closure."""


class MeetingEvidenceError(CollaborationError):
    """Raised when objections or decisions omit required concrete evidence."""


class DecisionSubstituteQaForbiddenError(CollaborationError):
    """Raised when attempting to treat a Master decision as a substitute for QA verification."""


class MeetingNotFoundError(CollaborationError):
    """Raised when querying or mutating a nonexistent meeting, conflict, or decision."""


class StaleCollaborationVersionError(CollaborationError):
    """Raised on optimistic lock conflict in SQLite persistence."""


class MeetingParticipant(BaseModel):
    """Bound meeting participant with explicit scoped role."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    role_in_meeting: Literal["chair", "contributor", "verifier"]
    step_ids: list[StepId] = Field(default_factory=list, max_length=16)


class MeetingComplexitySignal(BaseModel):
    """Structured task complexity evaluation deciding whether a real meeting is warranted."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    step_count: int = Field(ge=1)
    has_technical_disagreement: bool = False
    has_cross_step_conflict: bool = False
    has_high_risk_tradeoff: bool = False
    qa_failure_requires_triage: bool = False
    involved_step_ids: list[StepId] = Field(default_factory=list, max_length=32)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str) -> str:
        return ensure_not_secret(value, "complexity.reason")


class MeetingNecessityOutcome(BaseModel):
    """Outcome determining whether a meeting is opened or legitimately skipped for simple work."""

    model_config = ConfigDict(extra="forbid")

    meeting_required: bool
    reason: str
    eligible_participants: list[AgentId] = Field(default_factory=list)
    event_id: EventId | None = None


class MeetingTurnRecord(BaseModel):
    """One structured, evidence-grounded turn inside a real Multi-Agent meeting."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(default_factory=lambda: _new_prefixed_id("turn"), min_length=8, max_length=64)
    meeting_id: MeetingId
    round_number: int = Field(ge=1)
    turn_index: int = Field(ge=1)
    speaker_id: AgentId
    speaker_kind: ActorTrustKind
    turn_kind: MeetingTurnKind
    statement: str = Field(min_length=3, max_length=2000)
    target_turn_id: str | None = Field(default=None, min_length=4, max_length=64)
    evidence_references: list[str] = Field(default_factory=list, max_length=32)
    uncertainty_notes: list[str] = Field(default_factory=list, max_length=16)
    action_items: list[str] = Field(default_factory=list, max_length=16)
    consensus_claimed: bool = False
    timestamp: datetime

    @field_validator("statement", "target_turn_id")
    @classmethod
    def _clean_text(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "turn_field"))

    @field_validator("evidence_references", "uncertainty_notes", "action_items")
    @classmethod
    def _clean_lists(cls, values: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "turn_list")
        return [ensure_not_secret(item, field_name) for item in values]

    @field_validator("timestamp")
    @classmethod
    def _valid_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")

    @model_validator(mode="after")
    def _enforce_critique_rigor(self) -> MeetingTurnRecord:
        if self.turn_kind == "OBJECTION" and not self.evidence_references:
            raise ValueError("OBJECTION turn requires concrete evidence_references (no empty disagreement).")
        if self.turn_kind == "ACTION_ITEM" and not self.action_items:
            raise ValueError("ACTION_ITEM turn requires at least one concrete action item.")
        if self.turn_kind == "UNCERTAINTY" and not self.uncertainty_notes:
            raise ValueError("UNCERTAINTY turn requires explicit uncertainty_notes.")
        return self


class MasterDecisionRecord(BaseModel):
    """Master decision with user-visible rationale; never substitutes for QA verification."""

    model_config = ConfigDict(extra="forbid")

    decision_id: DecisionId = Field(default_factory=new_decision_id)
    meeting_id: MeetingId | None = None
    conflict_id: ConflictId | None = None
    task_id: TaskId
    run_id: RunId | None = None
    decided_by: AgentId
    chosen_option: str = Field(min_length=3, max_length=500)
    decision_summary: str = Field(min_length=3, max_length=1000)
    user_visible_rationale: str = Field(min_length=10, max_length=2000)
    acknowledged_dissent: list[str] = Field(default_factory=list, max_length=32)
    evidence_references: list[str] = Field(min_length=1, max_length=32)
    action_items: list[str] = Field(default_factory=list, max_length=32)
    can_substitute_qa: Literal[False] = False
    timestamp: datetime

    @field_validator("chosen_option", "decision_summary", "user_visible_rationale")
    @classmethod
    def _clean_text(cls, value: str, info: object) -> str:
        return ensure_not_secret(value, getattr(info, "field_name", "decision_field"))

    @field_validator("acknowledged_dissent", "evidence_references", "action_items")
    @classmethod
    def _clean_lists(cls, values: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "decision_list")
        return [ensure_not_secret(item, field_name) for item in values]

    @field_validator("timestamp")
    @classmethod
    def _valid_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class MeetingRecord(BaseModel):
    """Persisted structured Multi-Agent Meeting bound to Task, Run, Plan, and Team."""

    model_config = ConfigDict(extra="forbid")

    meeting_id: MeetingId = Field(default_factory=new_meeting_id)
    task_id: TaskId
    run_id: RunId
    plan_id: PlanId
    team_proposal_id: TeamProposalId
    agenda: str = Field(min_length=5, max_length=1000)
    chair_id: AgentId = "core.master"
    participants: list[MeetingParticipant] = Field(min_length=2, max_length=16)
    max_rounds: int = Field(default=3, ge=1, le=5)
    max_turns: int = Field(default=6, ge=2, le=20)
    turns: list[MeetingTurnRecord] = Field(default_factory=list)
    decision_ids: list[DecisionId] = Field(default_factory=list)
    status: MeetingStatus = "OPEN"
    correlation_id: str = Field(min_length=1, max_length=80)
    created_at: datetime
    decided_at: datetime | None = None
    closed_at: datetime | None = None

    @field_validator("agenda", "correlation_id")
    @classmethod
    def _clean_labels(cls, value: str, info: object) -> str:
        return ensure_not_secret(value, getattr(info, "field_name", "meeting_field"))

    @field_validator("created_at", "decided_at", "closed_at")
    @classmethod
    def _valid_ts(cls, value: datetime | None, info: object) -> datetime | None:
        return validate_aware_datetime(value, getattr(info, "field_name", "meeting_ts")) if value else None


class StoredMeeting(BaseModel):
    """Persisted MeetingRecord paired with optimistic locking counter."""

    model_config = ConfigDict(extra="forbid")

    meeting: MeetingRecord
    record_version: int = Field(ge=1)
    last_audit_event_id: EventId | None = None


class WorkerConflictRecord(BaseModel):
    """Recorded conflict when two Workers contend over the same file or step output."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: ConflictId = Field(default_factory=new_conflict_id)
    task_id: TaskId
    run_id: RunId
    conflict_type: ConflictType
    conflicting_path: str | None = Field(default=None, min_length=1, max_length=260)
    step_a_id: StepId
    agent_a_id: AgentId
    session_a_id: SessionId
    step_b_id: StepId
    agent_b_id: AgentId
    session_b_id: SessionId
    status: ConflictStatus = "OPEN"
    resolution_summary: str | None = Field(default=None, max_length=1000)
    decision_id: DecisionId | None = None
    handoff_id: HandoffId | None = None
    detected_at: datetime
    resolved_at: datetime | None = None

    @field_validator("conflicting_path")
    @classmethod
    def _valid_relpath(cls, value: str | None) -> str | None:
        return validate_safe_relative_path(value, "conflicting_path") if value is not None else None

    @field_validator("agent_a_id", "agent_b_id")
    @classmethod
    def _valid_workers(cls, value: str) -> str:
        cleaned = validate_agent_id(value)
        if cleaned == "core.master":
            raise ValueError("Worker conflict endpoints exclude core.master.")
        return cleaned

    @field_validator("resolution_summary")
    @classmethod
    def _clean_summary(cls, value: str | None) -> str | None:
        return ensure_not_secret(value, "resolution_summary") if value is not None else None

    @field_validator("detected_at", "resolved_at")
    @classmethod
    def _valid_ts(cls, value: datetime | None, info: object) -> datetime | None:
        return validate_aware_datetime(value, getattr(info, "field_name", "conflict_ts")) if value else None


class StoredWorkerConflict(BaseModel):
    """Persisted WorkerConflictRecord with optimistic lock version."""

    model_config = ConfigDict(extra="forbid")

    conflict: WorkerConflictRecord
    record_version: int = Field(ge=1)
    last_audit_event_id: EventId | None = None


class HandoffTimelineEntry(BaseModel):
    """Audited handoff paired with its immutable revision history."""

    model_config = ConfigDict(extra="forbid")

    handoff: StoredWorkerHandoff
    revisions: list[HandoffRevisionRecord]


class CollaborationHistoryReport(BaseModel):
    """Combined Worker-to-Worker handoff timeline and contention history."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    handoffs: list[HandoffTimelineEntry]
    conflicts: list[WorkerConflictRecord]


class DeliveryArtifactSummary(BaseModel):
    """Verified deliverable metadata and download reference for Master Delivery Summary."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: ArtifactId
    relative_path: str
    sha256_recorded: Sha256Hex | None = None
    sha256_actual: Sha256Hex | None = None
    disk_exists: bool
    integrity_matched: bool
    verification_status: ArtifactVerificationStatus
    evidence_reference: str | None = None
    download_ref: str = Field(min_length=5, max_length=512)


class MasterDeliverySummary(BaseModel):
    """Final Master delivery summary enforcing NOT_VERIFIED whenever proof is missing."""

    model_config = ConfigDict(extra="forbid")

    delivery_id: DeliveryId = Field(default_factory=new_delivery_id)
    task_id: TaskId
    run_id: RunId
    compiled_by: AgentId
    delivery_verification_status: DeliveryVerificationStatus
    qa_verified: bool
    verification_blockers: list[str] = Field(default_factory=list)
    artifacts: list[DeliveryArtifactSummary] = Field(default_factory=list)
    meeting_ids: list[MeetingId] = Field(default_factory=list)
    decision_ids: list[DecisionId] = Field(default_factory=list)
    handoff_ids: list[HandoffId] = Field(default_factory=list)
    open_conflict_ids: list[ConflictId] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=5, max_length=2000)
    compiled_at: datetime

    @field_validator("summary")
    @classmethod
    def _clean_summary(cls, value: str) -> str:
        return ensure_not_secret(value, "delivery.summary")

    @field_validator("verification_blockers", "known_limitations")
    @classmethod
    def _clean_lists(cls, values: list[str], info: object) -> list[str]:
        return [ensure_not_secret(item, getattr(info, "field_name", "delivery_list")) for item in values]

    @field_validator("compiled_at")
    @classmethod
    def _valid_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "compiled_at")

    @model_validator(mode="after")
    def _enforce_not_verified_without_proof(self) -> MasterDeliverySummary:
        """Fail closed: VERIFIED is impossible if blockers, missing proof, or open conflicts exist."""
        all_ok = (
            bool(self.artifacts)
            and not self.verification_blockers
            and not self.open_conflict_ids
            and all(
                a.disk_exists
                and a.integrity_matched
                and a.verification_status == "VERIFIED_PASS"
                and a.evidence_reference is not None
                for a in self.artifacts
            )
        )
        if self.delivery_verification_status == "VERIFIED" and (not self.qa_verified or not all_ok):
            raise ValueError("MasterDeliverySummary cannot claim VERIFIED without complete QA & disk SHA-256 proof.")
        if self.qa_verified and not all_ok:
            raise ValueError("qa_verified cannot be True when artifacts, QA proof, or conflict checks fail.")
        return self


class MeetingOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meeting_id: MeetingId | None = None
    task_id: TaskId
    run_id: RunId
    plan_id: PlanId
    team_proposal_id: TeamProposalId
    agenda: str = Field(min_length=5, max_length=1000)
    participant_agent_ids: list[AgentId] = Field(min_length=1, max_length=16)
    involved_step_ids: list[StepId] = Field(default_factory=list, max_length=16)
    actor_id: AgentId = "core.master"
    actor_kind: ActorTrustKind = "orchestrator"
    auth_context: TrustedAuthContext | None = None
    max_rounds: int = Field(default=3, ge=1, le=5)
    max_turns: int = Field(default=6, ge=2, le=20)
    timestamp: datetime
    correlation_id: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return validate_aware_datetime(v, "timestamp")


class MeetingTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meeting_id: MeetingId
    round_number: int = Field(ge=1)
    speaker_id: AgentId
    speaker_kind: ActorTrustKind = "worker_agent"
    auth_context: TrustedAuthContext | None = None
    turn_kind: MeetingTurnKind
    statement: str = Field(min_length=3, max_length=2000)
    target_turn_id: str | None = None
    evidence_references: list[str] = Field(default_factory=list, max_length=32)
    uncertainty_notes: list[str] = Field(default_factory=list, max_length=16)
    action_items: list[str] = Field(default_factory=list, max_length=16)
    consensus_claimed: bool = False
    timestamp: datetime
    correlation_id: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return validate_aware_datetime(v, "timestamp")


class MasterDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: DecisionId | None = None
    meeting_id: MeetingId | None = None
    conflict_id: ConflictId | None = None
    task_id: TaskId
    run_id: RunId | None = None
    actor_id: AgentId = "core.master"
    actor_kind: ActorTrustKind = "orchestrator"
    auth_context: TrustedAuthContext | None = None
    chosen_option: str = Field(min_length=3, max_length=500)
    decision_summary: str = Field(min_length=3, max_length=1000)
    user_visible_rationale: str = Field(min_length=10, max_length=2000)
    evidence_references: list[str] = Field(min_length=1, max_length=32)
    action_items: list[str] = Field(default_factory=list, max_length=32)
    close_meeting: bool = True
    substitute_qa: bool = False
    timestamp: datetime
    correlation_id: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return validate_aware_datetime(v, "timestamp")


class WorkerConflictCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: TaskId
    run_id: RunId
    step_a_id: StepId
    agent_a_id: AgentId
    session_a_id: SessionId
    paths_a: list[str] = Field(default_factory=list, max_length=32)
    step_b_id: StepId
    agent_b_id: AgentId
    session_b_id: SessionId
    paths_b: list[str] = Field(default_factory=list, max_length=32)
    timestamp: datetime
    correlation_id: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return validate_aware_datetime(v, "timestamp")


class WorkerConflictResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conflict_id: ConflictId
    actor_id: AgentId = "core.master"
    actor_kind: ActorTrustKind = "orchestrator"
    auth_context: TrustedAuthContext | None = None
    resolution_summary: str = Field(min_length=5, max_length=1000)
    decision_id: DecisionId | None = None
    handoff_id: HandoffId | None = None
    timestamp: datetime
    correlation_id: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return validate_aware_datetime(v, "timestamp")


class MasterDeliveryCompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivery_id: DeliveryId | None = None
    task_id: TaskId
    run_id: RunId
    actor_id: AgentId = "core.master"
    actor_kind: ActorTrustKind = "orchestrator"
    auth_context: TrustedAuthContext | None = None
    workspace_root: str = Field(min_length=1, max_length=512)
    artifact_ids: list[ArtifactId] = Field(default_factory=list, max_length=64)
    known_limitations: list[str] = Field(default_factory=list, max_length=32)
    summary: str = Field(min_length=5, max_length=2000)
    override_qa_unverified: bool = False
    timestamp: datetime
    correlation_id: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return validate_aware_datetime(v, "timestamp")


@runtime_checkable
class CollaborationRepository(Protocol):
    """Persistence port isolating Phase 06 collaboration services from SQLite details."""

    def create_meeting_atomic(self, *, meeting: MeetingRecord, event: Event) -> StoredMeeting:
        ...

    def update_meeting_atomic(
        self,
        *,
        meeting: MeetingRecord,
        expected_record_version: int,
        event: Event,
    ) -> StoredMeeting:
        ...

    def record_decision_atomic(
        self,
        *,
        decision: MasterDecisionRecord,
        updated_meeting: MeetingRecord | None,
        expected_meeting_version: int | None,
        events: list[Event],
    ) -> MasterDecisionRecord:
        ...

    def get_meeting(self, meeting_id: str) -> StoredMeeting | None:
        ...

    def list_meetings(self, *, task_id: str | None = None) -> list[StoredMeeting]:
        ...

    def list_decisions(self, *, task_id: str | None = None, meeting_id: str | None = None) -> list[MasterDecisionRecord]:
        ...

    def create_conflict_atomic(self, *, conflict: WorkerConflictRecord, event: Event) -> StoredWorkerConflict:
        ...

    def resolve_conflict_atomic(
        self,
        *,
        conflict: WorkerConflictRecord,
        expected_record_version: int,
        event: Event,
    ) -> StoredWorkerConflict:
        ...

    def get_conflict(self, conflict_id: str) -> StoredWorkerConflict | None:
        ...

    def list_conflicts(self, *, task_id: str | None = None, run_id: str | None = None) -> list[StoredWorkerConflict]:
        ...

    def save_delivery_atomic(self, *, delivery: MasterDeliverySummary, event: Event) -> MasterDeliverySummary:
        ...

    def get_delivery(self, delivery_id: str) -> MasterDeliverySummary | None:
        ...

    def list_deliveries(self, *, task_id: str | None = None) -> list[MasterDeliverySummary]:
        ...
