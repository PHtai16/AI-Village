"""Domain contracts, ports, and fail-closed exceptions for Phase 09 / P09.01 HR Department."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.agents.department import DepartmentManifest, DepartmentStatus
from core.agents.manifest import AgentCapabilities, AgentManifest, AgentMission
from core.agents.recruitment_models import RecruitmentId, StoredRecruitmentProposal
from core.agents.registry import AgentRegistrationStatus, CapabilityVerificationEntry
from core.contracts.agent import AgentSystemRole
from core.contracts.identifiers import (
    AgentId,
    CapabilityId,
    DepartmentId,
    EventId,
    SemVer,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
    validate_snake_id,
    validate_tool_id_syntax,
    validate_unique_id_list,
)
from core.governance.models import TrustedAuthContext
from core.tasks.transitions import ActorTrustKind
from engines.antigravity.diagnostics import redact_secrets


def _validate_skill_ids(values: list[str]) -> list[str]:
    """Validate unique snake_case skill identifiers without wildcards."""
    return validate_unique_id_list(
        values,
        lambda raw: validate_snake_id(raw, "skill_id"),
        "skill_ids",
    )


class HrDepartmentError(RuntimeError):
    """Base fail-closed exception raised by HR Department operations."""

    def __init__(self, error_code: str, message: str) -> None:
        cleaned = redact_secrets(message)
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned


class HrValidationError(HrDepartmentError):
    """Raised when HR operation inputs violate Master immutability, department, or capability rules."""


class HrStateError(HrDepartmentError):
    """Raised on no-op or invalid HR operational state (e.g. revoking ungranted skills)."""


class HrSkillApprovalError(HrDepartmentError):
    """Raised fail-closed when attempting to grant or propose an unapproved skill."""


class ApprovedSkillDescriptor(BaseModel):
    """Verified APPROVED Skill metadata exposed by Phase 10 Skill Factory or explicit catalog."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version: SemVer | None = None
    status: Literal["APPROVED"] = "APPROVED"
    required_capabilities: list[CapabilityId] = Field(default_factory=list, max_length=32)
    summary: str | None = Field(default=None, max_length=500)

    @field_validator("skill_id")
    @classmethod
    def _validate_skill_id(cls, value: str) -> str:
        """Require canonical snake_case skill_id without secrets."""
        return validate_snake_id(value, "skill_id")

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str | None) -> str | None:
        """Reject secret patterns in optional skill summary."""
        if value is None:
            return None
        return ensure_not_secret(value, "summary")


@runtime_checkable
class ApprovedSkillCatalogPort(Protocol):
    """Read-only port returning verified APPROVED skills for governed HR assignment."""

    def get_approved_skill(self, skill_id: str) -> ApprovedSkillDescriptor | None:
        """Return APPROVED descriptor for ``skill_id``, or None if missing/unapproved."""

    def list_approved_skills(self) -> list[ApprovedSkillDescriptor]:
        """Return deterministically sorted APPROVED skills available for assignment."""


class FailClosedEmptySkillCatalog:
    """Default empty skill catalog refusing all skill grants until Phase 10 catalog is bound."""

    def get_approved_skill(self, skill_id: str) -> ApprovedSkillDescriptor | None:
        """Always return None fail-closed."""
        return None

    def list_approved_skills(self) -> list[ApprovedSkillDescriptor]:
        """Always return an empty approved-skill list."""
        return []


class StaticApprovedSkillCatalog:
    """In-memory verified APPROVED skill catalog adapter for tests or pre-Phase-10 wiring."""

    def __init__(self, skills: list[ApprovedSkillDescriptor] | tuple[ApprovedSkillDescriptor, ...] = ()) -> None:
        """Index validated APPROVED skill descriptors by canonical ``skill_id``."""
        catalog: dict[str, ApprovedSkillDescriptor] = {}
        for item in skills:
            if not isinstance(item, ApprovedSkillDescriptor) or item.status != "APPROVED":
                raise HrSkillApprovalError(
                    "UNAPPROVED_SKILL",
                    "Skill catalog only accepts verified ApprovedSkillDescriptor with status='APPROVED'.",
                )
            catalog[item.skill_id] = item
        self._skills = catalog

    def get_approved_skill(self, skill_id: str) -> ApprovedSkillDescriptor | None:
        """Lookup validated APPROVED skill descriptor cleanly."""
        return self._skills.get(skill_id)

    def list_approved_skills(self) -> list[ApprovedSkillDescriptor]:
        """Return APPROVED descriptors sorted by ``skill_id``."""
        return [self._skills[k] for k in sorted(self._skills)]


@runtime_checkable
class RuntimeStateReader(Protocol):
    """Structural read-only port decoupling HR Explorer projection from ``core.orchestration``."""

    def get_worker_runtime_state(self, agent_id: str, *, task_id: str | None = None) -> Any:
        """Return runtime session/run observation snapshot for ``agent_id``."""


class HrAgentRuntimeProjection(BaseModel):
    """Truthful runtime observation snapshot for Explorer (never inferred from ACTIVE registry)."""

    model_config = ConfigDict(extra="forbid")

    runtime_state_source: Literal["NOT_OBSERVED", "RUNTIME_OBSERVER"] = "NOT_OBSERVED"
    task_id: TaskId | None = None
    session_id: str | None = None
    session_status: str | None = None
    session_initialized: bool = False
    session_active: bool = False
    observable_running: bool = False
    run_id: str | None = None
    run_status: str | None = None
    is_assigned_to_task: bool = False
    assigned_step_ids: list[str] = Field(default_factory=list)
    reconciliation_required: bool = False
    reconciliation_reason: str | None = None


class RoleChangeRequest(BaseModel):
    """Request to update an Agent's role, mission, capabilities, and optional department."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    new_role: str = Field(min_length=3, max_length=64)
    new_department: DepartmentId | None = None
    new_description: str | None = Field(default=None, min_length=3, max_length=1000)
    new_mission: AgentMission | None = None
    new_capabilities: AgentCapabilities | None = None
    validated_capabilities: list[CapabilityId] = Field(default_factory=list, max_length=64)
    expected_record_version: int = Field(ge=1)
    change_summary: str = Field(min_length=3, max_length=500)
    auth_context: TrustedAuthContext | None = None
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)
    timestamp: datetime
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("new_role", "new_description", "change_summary", "approval_id", "correlation_id")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        """Reject secret-bearing or blank strings."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "role_change"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "timestamp")


class DepartmentTransferRequest(BaseModel):
    """Request to transfer an existing Worker Agent to another active Department."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    target_department: DepartmentId
    new_role: str | None = Field(default=None, min_length=3, max_length=64)
    new_description: str | None = Field(default=None, min_length=3, max_length=1000)
    new_mission: AgentMission | None = None
    new_capabilities: AgentCapabilities | None = None
    validated_capabilities: list[CapabilityId] = Field(default_factory=list, max_length=64)
    expected_record_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)
    auth_context: TrustedAuthContext | None = None
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)
    timestamp: datetime
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("new_role", "new_description", "reason", "approval_id", "correlation_id")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        """Reject secret-bearing or blank strings."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "transfer_request"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "timestamp")


class SkillGrantRequest(BaseModel):
    """Request to grant APPROVED skills into an Agent manifest's ``skills.allowed``."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    skill_ids: list[str] = Field(min_length=1, max_length=32)
    expected_record_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)
    auth_context: TrustedAuthContext | None = None
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)
    timestamp: datetime
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("skill_ids")
    @classmethod
    def _validate_skills(cls, values: list[str]) -> list[str]:
        """Validate deduplicated snake_case skill identifiers."""
        return _validate_skill_ids(values)

    @field_validator("reason", "approval_id", "correlation_id")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        """Reject secret-bearing or blank strings."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "skill_grant"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "timestamp")


class SkillRevokeRequest(BaseModel):
    """Request to revoke skills from an Agent manifest's ``skills.allowed``."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    skill_ids: list[str] = Field(min_length=1, max_length=32)
    expected_record_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)
    auth_context: TrustedAuthContext | None = None
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)
    timestamp: datetime
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("skill_ids")
    @classmethod
    def _validate_skills(cls, values: list[str]) -> list[str]:
        """Validate deduplicated snake_case skill identifiers."""
        return _validate_skill_ids(values)

    @field_validator("reason", "approval_id", "correlation_id")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        """Reject secret-bearing or blank strings."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "skill_revoke"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "timestamp")


class CapabilityGapRequest(BaseModel):
    """Criteria used by HR to check active workforce coverage vs missing capabilities."""

    model_config = ConfigDict(extra="forbid")

    required_capabilities: list[CapabilityId] = Field(min_length=1, max_length=32)
    department: DepartmentId | None = None
    required_tools: list[str] = Field(default_factory=list, max_length=32)
    include_unvalidated: bool = False

    @field_validator("required_tools")
    @classmethod
    def _validate_tools(cls, values: list[str]) -> list[str]:
        """Validate canonical tool IDs cleanly."""
        return validate_unique_id_list(values, validate_tool_id_syntax, "required_tools")


class CapabilityGapAssessment(BaseModel):
    """Read-only capability coverage report driving HR recruitment recommendations."""

    model_config = ConfigDict(extra="forbid")

    department: DepartmentId | None = None
    required_capabilities: list[CapabilityId]
    required_tools: list[str] = Field(default_factory=list)
    covered_capabilities: list[CapabilityId]
    missing_capabilities: list[CapabilityId]
    eligible_agent_ids: list[AgentId]
    recommended_recruitment: bool
    department_supported_capabilities: list[CapabilityId] = Field(default_factory=list)
    department_advisory_tools: list[str] = Field(default_factory=list)


class HrRecruitmentDraftRequest(BaseModel):
    """HR recruitment intake request delegated atomically to ``AgentRecruitmentService``."""

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
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        """Reject secret-bearing or blank strings."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "hr_recruitment"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "timestamp")


class HrExplorerQuery(BaseModel):
    """Filter criteria for read-only HR Agent Explorer metadata export."""

    model_config = ConfigDict(extra="forbid")

    include_disabled: bool = True
    department: DepartmentId | None = None
    registration_status: AgentRegistrationStatus | None = None
    task_id: TaskId | None = None


class HrAgentExplorerItem(BaseModel):
    """Unified HR status, capability, permission, department, and runtime view for Explorer."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    name: str
    role: str
    system_role: AgentSystemRole
    department: DepartmentId
    department_name: str | None = None
    department_zone_id: str | None = None
    department_status: DepartmentStatus | None = None
    registration_status: AgentRegistrationStatus
    active_revision: int = Field(ge=1)
    record_version: int = Field(ge=1)
    manifest_sha256: Sha256Hex
    source_path: str | None = None
    drift_status: str | None = None
    capabilities: list[CapabilityVerificationEntry]
    declared_skills: list[CapabilityId]
    declared_inputs: list[str]
    declared_outputs: list[str]
    skills_allowed: list[str]
    tools_allowed: list[str]
    department_advisory_tools: list[str] = Field(default_factory=list)
    engine_preferences: dict[str, str | None]
    permission_summary: dict[str, Any]
    open_recruitment_proposal_ids: list[str] = Field(default_factory=list)
    updated_at: datetime
    last_audit_event_id: EventId | None = None
    runtime: HrAgentRuntimeProjection


class HrExplorerCatalog(BaseModel):
    """Top-level HR metadata snapshot exported to Agent Explorer and Mission Control."""

    model_config = ConfigDict(extra="forbid")

    generated_for_task_id: TaskId | None = None
    agents: list[HrAgentExplorerItem]
    departments: list[DepartmentManifest] = Field(default_factory=list)
    open_recruitments: list[StoredRecruitmentProposal] = Field(default_factory=list)
    approved_skills_available: list[str] = Field(default_factory=list)
