"""Domain contracts, pinning records, and persistence port for P05.04 Dynamic Team Builder."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Annotated, Literal, Protocol, Sequence, runtime_checkable
import uuid

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    CapabilityId,
    ChecklistItemId,
    DepartmentId,
    EventId,
    SemVer,
    Sha256Hex,
    SnakeId,
    TaskId,
    ensure_not_secret,
    reject_secret_text,
    validate_agent_id,
    validate_aware_datetime,
    validate_capability_id,
    validate_unique_id_list,
)
from core.contracts.references import ChecklistItem
from core.contracts.task import Task
from core.orchestration.planning_models import DataResidencyPolicy, PlanId, StepId
from engines.antigravity.diagnostics import redact_secrets

_TEAM_PROPOSAL_ID_RE = re.compile(r"^team_[a-z0-9][a-z0-9_-]{3,63}$")

CapabilityVerificationRequirement = Literal["declared", "validated", "observed_in_execution"]
TeamProposalStatus = Literal[
    "READY_FOR_ASSIGNMENT",
    "PARTIAL",
    "BLOCKED_MISSING_CAPABILITY",
    "BLOCKED_GOVERNANCE",
    "ASSIGNED",
    "STALE",
    "SUPERSEDED",
]
StepAssignmentStatus = Literal[
    "ASSIGNED_IN_PROPOSAL",
    "MISSING_CAPABILITY",
    "BLOCKED_GOVERNANCE",
]
StalenessIssueCode = Literal[
    "AGENT_MISSING",
    "AGENT_NOT_ACTIVE",
    "MANIFEST_REVISION_DRIFT",
    "MANIFEST_HASH_DRIFT",
]


def new_team_proposal_id() -> str:
    """Generate a unique canonical Dynamic Team Proposal identifier (`team_*`)."""
    return f"team_{uuid.uuid4().hex[:16]}"


def validate_team_proposal_id(value: str) -> str:
    """Validate canonical `team_*` TeamProposal identifier format without secrets."""
    cleaned = ensure_not_secret(value, "proposal_id")
    if not _TEAM_PROPOSAL_ID_RE.fullmatch(cleaned):
        raise ValueError(
            f"proposal_id must match '^team_[a-z0-9][a-z0-9_-]{{3,63}}$', got {value!r}."
        )
    return cleaned


TeamProposalId = Annotated[str, AfterValidator(validate_team_proposal_id)]


class TeamBuilderError(RuntimeError):
    """Base fail-closed exception raised by Dynamic Team Builder operations."""

    def __init__(self, error_code: str, message: str) -> None:
        cleaned = redact_secrets(message)
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned


class TeamGovernanceDeniedError(TeamBuilderError):
    """Raised when Governance Policy denies team evaluation or assignment."""


class TeamApprovalRequiredError(TeamBuilderError):
    """Raised when Governance requires explicit Human Approval before assignment."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        approval_request_id: str | None = None,
    ) -> None:
        super().__init__(error_code, message)
        self.approval_request_id = approval_request_id


class TeamAssignmentStateError(TeamBuilderError):
    """Raised when Task or Proposal status forbids assignment."""


class StaleTeamProposalError(TeamBuilderError):
    """Raised when a Team Proposal references outdated or disabled Agent Manifests or Plans."""


class TeamProposalNotFoundError(TeamBuilderError):
    """Raised when querying or mutating a nonexistent `proposal_id`."""


class PinnedAgentAssignment(BaseModel):
    """Selected Worker assignment pinned to an exact Registry Manifest revision and hash."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    department: DepartmentId
    manifest_revision: int = Field(ge=1)
    manifest_sha256: Sha256Hex
    spec_version: SemVer
    engine_id: SnakeId
    provider_id: SnakeId | None = None
    connection_id: SnakeId | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    assigned_step_ids: list[StepId] = Field(min_length=1, max_length=200)
    matched_capabilities: list[CapabilityId] = Field(min_length=1, max_length=64)
    capability_evidence_levels: dict[CapabilityId, CapabilityVerificationRequirement] = Field(
        default_factory=dict
    )
    selection_reason: str = Field(min_length=3, max_length=500)
    quota_remaining: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)

    @field_validator("agent_id")
    @classmethod
    def _forbid_master_as_worker(cls, value: str) -> str:
        """Strictly prevent assigning core.master as a Worker team member."""
        cleaned = validate_agent_id(value)
        if cleaned == "core.master":
            raise ValueError("core.master is an orchestrator and cannot be assigned as a Worker.")
        return cleaned

    @field_validator("model_id", "selection_reason")
    @classmethod
    def _validate_safe_text(cls, value: str | None, info: object) -> str | None:
        """Ensure descriptive and routing labels are non-blank and secret-free."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "pinned_member"))

    @field_validator("matched_capabilities")
    @classmethod
    def _validate_matched_caps(cls, values: list[str]) -> list[str]:
        """Deduplicate and validate capability identifiers."""
        return validate_unique_id_list(values, validate_capability_id, "matched_capabilities")


class StepAssignmentEntry(BaseModel):
    """Assignment result for a single MasterWorkPlan step."""

    model_config = ConfigDict(extra="forbid")

    step_id: StepId
    checklist_item_id: ChecklistItemId | None = None
    required_capabilities: list[CapabilityId] = Field(min_length=1)
    assigned_agent_id: AgentId | None = None
    pinned_manifest_revision: int | None = Field(default=None, ge=1)
    status: StepAssignmentStatus
    missing_capabilities: list[CapabilityId] = Field(default_factory=list)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("assigned_agent_id")
    @classmethod
    def _forbid_master_step_owner(cls, value: str | None) -> str | None:
        """Prevent core.master from owning execution steps."""
        if value is None:
            return None
        cleaned = validate_agent_id(value)
        if cleaned == "core.master":
            raise ValueError("core.master cannot own a Worker execution step.")
        return cleaned

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        """Reject secret patterns or blank assignment rationale."""
        return ensure_not_secret(value, "step_assignment.reason")


class CandidateEvaluation(BaseModel):
    """Transparent audit entry detailing eligibility checks for each Registry Agent."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    department: DepartmentId
    registration_status: str = Field(min_length=1, max_length=32)
    manifest_revision: int = Field(ge=1)
    eligible: bool
    matched_capabilities: list[CapabilityId] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    exclusion_reasons: list[str] = Field(default_factory=list)


class TeamGovernanceCheck(BaseModel):
    """Recorded Governance check outcome associated with team building or assignment."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=64)
    outcome: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    reason: str = Field(min_length=1, max_length=500)
    approval_request_id: str | None = None

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        """Reject secret patterns inside Governance rationale."""
        return ensure_not_secret(value, "governance_check.reason")


class TeamProposal(BaseModel):
    """Capability-matched, manifest-pinned dynamic team specification."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: TeamProposalId = Field(default_factory=new_team_proposal_id)
    task_id: TaskId
    plan_id: PlanId
    plan_revision: int = Field(ge=1)
    proposal_revision: int = Field(default=1, ge=1)
    status: TeamProposalStatus
    min_capability_level: CapabilityVerificationRequirement = "declared"
    pinned_members: list[PinnedAgentAssignment] = Field(default_factory=list)
    step_assignments: list[StepAssignmentEntry] = Field(min_length=1)
    candidate_evaluations: list[CandidateEvaluation] = Field(default_factory=list)
    missing_capabilities: list[CapabilityId] = Field(default_factory=list)
    departments_involved: list[DepartmentId] = Field(default_factory=list)
    selection_summary: str = Field(min_length=3, max_length=1000)
    governance_checks: list[TeamGovernanceCheck] = Field(default_factory=list)
    created_by: AgentId = "core.master"
    created_at: datetime
    change_summary: str = Field(
        default="Initial capability-matched dynamic team proposal",
        min_length=1,
        max_length=500,
    )

    @field_validator("selection_summary", "change_summary")
    @classmethod
    def _validate_safe_labels(cls, value: str, info: object) -> str:
        """Reject secret material and blank text."""
        return ensure_not_secret(value, getattr(info, "field_name", "team_proposal"))

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "created_at")

    @model_validator(mode="after")
    def _validate_pinning_consistency(self) -> TeamProposal:
        """Ensure pinned_members are unique Workers aligned with step_assignments."""
        pinned_map: dict[str, PinnedAgentAssignment] = {}
        for member in self.pinned_members:
            if member.agent_id in pinned_map:
                raise ValueError(f"Duplicate pinned member '{member.agent_id}' in TeamProposal.")
            pinned_map[member.agent_id] = member

        for entry in self.step_assignments:
            if entry.status == "ASSIGNED_IN_PROPOSAL":
                if entry.assigned_agent_id is None or entry.pinned_manifest_revision is None:
                    raise ValueError(
                        f"Assigned step '{entry.step_id}' requires both assigned_agent_id and pinned_manifest_revision."
                    )
                if entry.assigned_agent_id not in pinned_map:
                    raise ValueError(
                        f"Assigned agent '{entry.assigned_agent_id}' for step '{entry.step_id}' missing from pinned_members."
                    )
                if pinned_map[entry.assigned_agent_id].manifest_revision != entry.pinned_manifest_revision:
                    raise ValueError(
                        f"Step '{entry.step_id}' revision pin does not match pinned_members entry."
                    )
        return self

    def canonical_json(self) -> str:
        """Return deterministic compact JSON representation."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def compute_sha256(self) -> str:
        """Return 64-char SHA-256 hex digest of canonical proposal JSON."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class StoredTeamProposal(BaseModel):
    """Durable SQLite envelope storing active TeamProposal snapshot and optimistic version."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: TeamProposalId
    task_id: TaskId
    plan_id: PlanId
    plan_revision: int = Field(ge=1)
    active_revision: int = Field(ge=1)
    record_version: int = Field(ge=1)
    status: TeamProposalStatus
    proposal: TeamProposal
    proposal_sha256: Sha256Hex
    created_by: AgentId
    created_at: datetime
    updated_by: AgentId
    updated_at: datetime
    last_audit_event_id: EventId

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_ts(cls, value: datetime, info: object) -> datetime:
        """Require explicit timezone-aware timestamps."""
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))


class TeamProposalRevisionRecord(BaseModel):
    """Immutable historical snapshot of one TeamProposal revision."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: TeamProposalId
    task_id: TaskId
    proposal_revision: int = Field(ge=1)
    status: TeamProposalStatus
    proposal: TeamProposal
    proposal_sha256: Sha256Hex
    change_summary: str = Field(min_length=1, max_length=500)
    changed_by: AgentId
    changed_at: datetime
    audit_event_id: EventId

    @field_validator("change_summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        """Reject secret patterns or blank summary text."""
        return ensure_not_secret(value, "change_summary")

    @field_validator("changed_at")
    @classmethod
    def _validate_changed_at(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "changed_at")


class AgentStalenessIssue(BaseModel):
    """Mismatch detected between a pinned assignment and live Agent Registry state."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    pinned_revision: int = Field(ge=1)
    current_revision: int | None = None
    current_status: str | None = None
    issue_code: StalenessIssueCode
    detail: str = Field(min_length=3, max_length=500)

    @field_validator("detail")
    @classmethod
    def _validate_detail(cls, value: str) -> str:
        """Reject secret patterns inside staleness details."""
        cleaned = value.strip()
        reject_secret_text(cleaned, "staleness_detail")
        return cleaned


class TeamStalenessReport(BaseModel):
    """Verification result checking pinned manifest revisions against Registry."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: TeamProposalId
    task_id: TaskId
    is_stale: bool
    checked_at: datetime
    issues: list[AgentStalenessIssue] = Field(default_factory=list)

    @field_validator("checked_at")
    @classmethod
    def _validate_checked_at(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "checked_at")


class TeamBuildRequest(BaseModel):
    """Input criteria constraining deterministic candidate selection for a plan."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    plan_id: PlanId
    min_capability_level: CapabilityVerificationRequirement = "declared"
    allowed_providers: list[SnakeId] | None = None
    allowed_models: list[str] | None = None
    required_data_residency: DataResidencyPolicy | None = None
    max_team_size: int | None = Field(default=None, ge=1, le=50)
    change_summary: str = Field(
        default="Dynamic capability-based team proposal",
        min_length=1,
        max_length=500,
    )

    @field_validator("change_summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        """Reject secret strings in build summary."""
        return ensure_not_secret(value, "change_summary")

    @field_validator("allowed_models")
    @classmethod
    def _validate_models(cls, values: list[str] | None) -> list[str] | None:
        """Ensure model allowlist entries are clean strings."""
        if values is None:
            return None
        return [ensure_not_secret(item, "allowed_models") for item in values]


class TeamAssignmentResult(BaseModel):
    """Structured output confirming atomic Task and Checklist assignment without execution."""

    model_config = ConfigDict(extra="forbid")

    stored_proposal: StoredTeamProposal
    task: Task
    checklist_items: list[ChecklistItem]
    audit_event_ids: list[EventId] = Field(default_factory=list)


@runtime_checkable
class TeamProposalRepository(Protocol):
    """Persistence port isolating ``DynamicTeamBuilderService`` from SQLite details."""

    def create_proposal(
        self,
        record: StoredTeamProposal,
        initial_revision: TeamProposalRevisionRecord,
        audit_event: Event,
        *,
        extra_events: Sequence[Event] = (),
    ) -> StoredTeamProposal:
        """Atomically persist Event(s), active proposal, and immutable revision v1."""

    def revise_proposal(
        self,
        record: StoredTeamProposal,
        new_revision: TeamProposalRevisionRecord,
        *,
        expected_record_version: int,
        audit_event: Event,
        extra_events: Sequence[Event] = (),
    ) -> StoredTeamProposal:
        """Atomically update active proposal under optimistic lock and append revision."""

    def get_proposal(self, proposal_id: str) -> StoredTeamProposal | None:
        """Load active proposal snapshot by ``proposal_id`` or return None."""

    def get_active_proposal_for_task(self, task_id: str) -> StoredTeamProposal | None:
        """Load latest proposal bound to ``task_id`` or return None."""

    def list_proposal_revisions(self, proposal_id: str) -> list[TeamProposalRevisionRecord]:
        """Return ascending immutable revision history for ``proposal_id``."""

    def apply_team_assignment(
        self,
        *,
        proposal_id: str,
        expected_proposal_record_version: int,
        updated_proposal: StoredTeamProposal,
        new_revision: TeamProposalRevisionRecord,
        assigned_agent_ids: list[str],
        step_agent_map: dict[str, str],
        actor_id: str,
        actor_kind: str,
        timestamp: datetime,
        team_event: Event,
    ) -> TeamAssignmentResult:
        """Atomically assign team onto Task & PENDING ChecklistItems without starting a Run."""
