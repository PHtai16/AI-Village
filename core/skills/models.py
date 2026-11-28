"""Domain contracts, 7-state lifecycle, ports, and fail-closed errors for Phase 10 Skill Factory."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal, Protocol, runtime_checkable
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    EventId,
    EvidenceId,
    RunId,
    SemVer,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
    validate_semver,
    validate_sha256_hex,
    validate_snake_id,
    validate_tool_id_syntax,
    validate_unique_id_list,
)
from core.departments._common import DepartmentBehaviorError
from core.tools.models import ToolInvocationRequest, ToolResult
from engines.antigravity.diagnostics import redact_secrets

SkillLifecycleStatus = Literal[
    "DRAFT",
    "CANDIDATE",
    "UNDER_REVIEW",
    "APPROVED",
    "REJECTED",
    "DEPRECATED",
    "DISABLED",
]
BenchmarkStatus = Literal[
    "NOT_BENCHMARKED",
    "IN_PROGRESS",
    "PASSED",
    "FAILED",
    "INCONCLUSIVE",
]
SkillRiskLevel = Literal["low", "medium", "high"]
SkillEngineTier = Literal["flash", "pro", "code"]
SkillMaintainer = Literal["it", "process", "qa"]
SkillStorageZone = Literal["candidates", "approved"]
SkillProposalStatus = Literal["PENDING_APPROVAL", "PENDING_ACTIVATION", "ACTIVATED", "REJECTED"]

VALID_SKILL_TRANSITIONS: dict[SkillLifecycleStatus, frozenset[SkillLifecycleStatus]] = {
    "DRAFT": frozenset({"CANDIDATE", "REJECTED"}),
    "CANDIDATE": frozenset({"UNDER_REVIEW", "REJECTED", "DRAFT"}),
    "UNDER_REVIEW": frozenset({"APPROVED", "REJECTED", "CANDIDATE"}),
    "APPROVED": frozenset({"DEPRECATED", "DISABLED"}),
    "REJECTED": frozenset({"DRAFT", "CANDIDATE"}),
    "DEPRECATED": frozenset({"APPROVED", "DISABLED"}),
    "DISABLED": frozenset({"DEPRECATED"}),
}

SENSITIVE_SKILL_TOOLS: frozenset[str] = frozenset(
    {
        "tools.files.write",
        "tools.files.delete",
        "tools.code.run_sandbox",
        "tools.code.git_diff",
        "tools.research.web_search",
        "tools.research.web_fetch",
        "tools.browser.open_session",
        "tools.browser.screenshot",
        "tools.browser.interact",
        "tools.mcp.call",
    }
)

_PACKAGE_ID_RE = re.compile(r"^skp_[a-z0-9]{8,48}$")


def new_package_id() -> str:
    """Generate canonical persistent Skill package identifier (`skp_*`)."""
    return f"skp_{uuid.uuid4().hex[:14]}"


def new_it_review_id() -> str:
    """Generate canonical IT technical review identifier (`itr_*`)."""
    return f"itr_{uuid.uuid4().hex[:14]}"


def new_benchmark_id() -> str:
    """Generate canonical QA A/B benchmark identifier (`bmk_*`)."""
    return f"bmk_{uuid.uuid4().hex[:14]}"


def new_activation_proposal_id() -> str:
    """Generate canonical Master skill activation proposal identifier (`sap_*`)."""
    return f"sap_{uuid.uuid4().hex[:14]}"


def new_improvement_outcome_id() -> str:
    """Generate canonical post-activation improvement outcome identifier (`sko_*`)."""
    return f"sko_{uuid.uuid4().hex[:14]}"


def _scrub(value: str, *, max_length: int = 1000) -> str:
    return redact_secrets(str(value)).strip()[:max_length]


class SkillError(DepartmentBehaviorError):
    """Base fail-closed exception raised across Phase 10 Skill Factory operations."""


class SkillValidationError(SkillError):
    """Raised when SKILL.md manifest, contract, SemVer, or path structure is invalid."""


class SkillStateError(SkillError):
    """Raised on invalid lifecycle transitions, missing package, or stale version conflicts."""


class SkillIndependenceError(SkillError):
    """Raised when an author or IT reviewer attempts independent QA self-certification."""


class SkillBenchmarkError(SkillError):
    """Raised when benchmark fixtures fail validation or activation lacks passing QA benchmark."""


class SkillGovernanceError(SkillError):
    """Raised when Governance denies or requires unfulfilled human approval."""


class SkillIntegrityError(SkillError):
    """Raised when approved Skill files on disk are missing or SHA-256 checksum mismatches."""


class SkillRequirements(BaseModel):
    """Structured tool, dependency, and minimum engine tier declared by a Skill."""

    model_config = ConfigDict(extra="forbid")

    tools: list[str] = Field(default_factory=list, max_length=32)
    dependencies: list[str] = Field(default_factory=list, max_length=32)
    min_engine_tier: SkillEngineTier | None = None

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, values: list[str]) -> list[str]:
        return validate_unique_id_list(values, validate_tool_id_syntax, "requirements.tools")

    @field_validator("dependencies")
    @classmethod
    def _validate_deps(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in values:
            safe = ensure_not_secret(item, "requirements.dependencies").strip()
            if not safe:
                raise ValueError("Dependency identifier cannot be blank.")
            if safe not in seen:
                seen.add(safe)
                cleaned.append(safe)
        return cleaned


class SkillFrontmatter(BaseModel):
    """YAML frontmatter schema complying with ``SKILL_SPEC.md`` Section 3.3."""

    model_config = ConfigDict(extra="forbid")

    spec_version: SemVer = "1.0.0"
    id: str
    name: str = Field(min_length=3, max_length=64)
    version: SemVer
    description: str = Field(min_length=3, max_length=500)
    category: str
    author: str = Field(min_length=2, max_length=64)
    maintainer: SkillMaintainer
    status: SkillLifecycleStatus = "DRAFT"
    requirements: SkillRequirements

    @field_validator("id", "category")
    @classmethod
    def _validate_snake(cls, value: str, info: object) -> str:
        return validate_snake_id(value, getattr(info, "field_name", "skill_id"))

    @field_validator("name", "description", "author")
    @classmethod
    def _validate_text(cls, value: str, info: object) -> str:
        return ensure_not_secret(value, getattr(info, "field_name", "frontmatter"))


class SkillIOField(BaseModel):
    """Explicit input or output parameter contract of a Skill."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    data_type: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=2, max_length=300)
    required: bool = True

    @field_validator("name", "data_type", "description")
    @classmethod
    def _validate_io_text(cls, value: str, info: object) -> str:
        return ensure_not_secret(value, getattr(info, "field_name", "skill_io"))


class SkillExecutionContract(BaseModel):
    """Ten mandatory Execution Contract sections per ``SKILL_SPEC.md`` Section 4."""

    model_config = ConfigDict(extra="forbid")

    purpose_and_scope: str = Field(min_length=5, max_length=1500)
    triggers: list[str] = Field(min_length=1, max_length=20)
    anti_triggers: list[str] = Field(min_length=1, max_length=20)
    inputs: list[SkillIOField] = Field(min_length=1, max_length=20)
    outputs: list[SkillIOField] = Field(min_length=1, max_length=20)
    execution_steps: list[str] = Field(min_length=1, max_length=30)
    security_and_permissions: list[str] = Field(min_length=1, max_length=20)
    definition_of_done: list[str] = Field(min_length=1, max_length=20)
    evidence_requirements: list[str] = Field(min_length=1, max_length=20)
    error_handling: list[str] = Field(min_length=1, max_length=20)

    @field_validator("purpose_and_scope")
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        return ensure_not_secret(value, "purpose_and_scope")

    @field_validator(
        "triggers",
        "anti_triggers",
        "execution_steps",
        "security_and_permissions",
        "definition_of_done",
        "evidence_requirements",
        "error_handling",
    )
    @classmethod
    def _validate_str_lists(cls, values: list[str], info: object) -> list[str]:
        fname = getattr(info, "field_name", "contract_list")
        return [ensure_not_secret(v, fname) for v in values]


class SkillRiskProfile(BaseModel):
    """Explicit risk and privilege sensitivity assessment computed for a Skill candidate."""

    model_config = ConfigDict(extra="forbid")

    risk_level: SkillRiskLevel = "low"
    risks: list[str] = Field(min_length=1, max_length=20)
    modifies_permissions_or_tools: bool = False
    modifies_sensitive_workflow: bool = False
    has_executable_scripts: bool = False
    requires_human_approval: bool = False
    sensitivity_reasons: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("risks", "sensitivity_reasons")
    @classmethod
    def _validate_risk_text(cls, values: list[str], info: object) -> list[str]:
        return [ensure_not_secret(v, getattr(info, "field_name", "risks")) for v in values]


class SkillTestCaseSpec(BaseModel):
    """Fixture case specification packaged inside ``{skill_id}/tests/``."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=2, max_length=64)
    case_kind: Literal["happy_path", "failure_case", "edge_case"]
    description: str = Field(min_length=3, max_length=500)
    expected_outcome: str = Field(min_length=2, max_length=300)
    input_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("case_id", "description", "expected_outcome")
    @classmethod
    def _validate_case_fields(cls, value: str, info: object) -> str:
        return ensure_not_secret(value, getattr(info, "field_name", "test_case"))


class SkillCandidateRequest(BaseModel):
    """Request to create/package a versioned P10.03 Skill candidate in ``candidates/``."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    name: str = Field(min_length=3, max_length=64)
    version: SemVer = "1.0.0"
    spec_version: SemVer = "1.0.0"
    description: str = Field(min_length=3, max_length=500)
    category: str
    author: str = Field(min_length=2, max_length=64)
    maintainer: SkillMaintainer = "process"
    source_improvement_id: str | None = Field(default=None, min_length=8, max_length=64)
    requirements: SkillRequirements
    contract: SkillExecutionContract
    risk_level: SkillRiskLevel = "low"
    risks: list[str] = Field(min_length=1, max_length=20)
    modifies_sensitive_workflow: bool = False
    test_cases: list[SkillTestCaseSpec] = Field(min_length=2, max_length=32)
    scripts: dict[str, str] = Field(default_factory=dict, max_length=16)
    references: dict[str, str] = Field(default_factory=dict, max_length=16)
    submit_as_candidate: bool = True
    timestamp: datetime

    @field_validator("skill_id", "category")
    @classmethod
    def _validate_ids(cls, value: str, info: object) -> str:
        return validate_snake_id(value, getattr(info, "field_name", "skill_id"))

    @field_validator("name", "description", "author", "source_improvement_id")
    @classmethod
    def _validate_strings(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "skill_candidate"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class SkillPackageRecord(BaseModel):
    """Canonical persistent representation of one immutable Skill version package."""

    model_config = ConfigDict(extra="forbid")

    package_id: str
    skill_id: str
    version: SemVer
    lifecycle_status: SkillLifecycleStatus
    frontmatter: SkillFrontmatter
    contract: SkillExecutionContract
    risk_profile: SkillRiskProfile
    test_cases: list[SkillTestCaseSpec] = Field(default_factory=list)
    source_improvement_id: str | None = None
    storage_zone: SkillStorageZone = "candidates"
    package_relpath: str
    manifest_sha256: Sha256Hex
    package_sha256: Sha256Hex
    file_hashes: dict[str, Sha256Hex] = Field(default_factory=dict)
    benchmark_status: BenchmarkStatus = "NOT_BENCHMARKED"
    latest_benchmark_id: str | None = None
    it_review_passed: bool = False
    latest_it_review_id: str | None = None
    approved_for_production: bool = False
    record_version: int = Field(default=1, ge=1)
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    last_audit_event_id: EventId

    @field_validator("package_id")
    @classmethod
    def _validate_pkg_id(cls, value: str) -> str:
        cleaned = ensure_not_secret(value, "package_id")
        if not _PACKAGE_ID_RE.fullmatch(cleaned):
            raise ValueError(f"package_id must match '^skp_[a-z0-9]{{8,48}}$', got {value!r}.")
        return cleaned

    @field_validator("manifest_sha256", "package_sha256")
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        return validate_sha256_hex(value)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_dts(cls, value: datetime, info: object) -> datetime:
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))


class SkillActivePointer(BaseModel):
    """Current active and previous APPROVED version pointers enabling atomic rollback."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    active_version: SemVer | None = None
    previous_approved_version: SemVer | None = None
    updated_by: str
    updated_at: datetime
    last_audit_event_id: EventId


class ItTechnicalReviewRequest(BaseModel):
    """Request recording P10.04 IT technical validation and sandbox readiness on a candidate."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version: SemVer
    task_id: TaskId | None = None
    run_id: RunId | None = None
    sandbox_passed: bool
    sandbox_stdout: str = Field(default="", max_length=4000)
    sandbox_stderr: str = Field(default="", max_length=4000)
    findings: list[str] = Field(default_factory=list, max_length=20)
    timestamp: datetime

    @field_validator("skill_id")
    @classmethod
    def _validate_sid(cls, value: str) -> str:
        return validate_snake_id(value, "skill_id")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class SkillItReviewRecord(BaseModel):
    """Persisted P10.04 IT technical compatibility and sandbox verification record."""

    model_config = ConfigDict(extra="forbid")

    review_id: str
    skill_id: str
    version: SemVer
    package_sha256: Sha256Hex
    reviewer_id: str
    passed: bool
    registered_tools_verified: list[str] = Field(default_factory=list)
    missing_tools: list[str] = Field(default_factory=list)
    scripts_checked: list[str] = Field(default_factory=list)
    sandbox_passed: bool
    sandbox_log_excerpt: str = ""
    findings: list[str] = Field(default_factory=list)
    reviewed_at: datetime
    audit_event_id: EventId


class SkillBenchmarkCaseOutcome(BaseModel):
    """Single deterministic fixture run outcome measured cleanly for Group A or Group B."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    case_kind: Literal["happy_path", "failure_case", "edge_case"]
    arm: Literal["A_BASELINE", "B_SKILL"]
    passed: bool
    duration_ms: int = Field(ge=0)
    tool_calls_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0.0)
    output_sha256: Sha256Hex
    notes: str = ""


class SkillBenchmarkMetrics(BaseModel):
    """Honest comparative A/B benchmark summary computed strictly from measured fixture arms."""

    model_config = ConfigDict(extra="forbid")

    cases_count: int = Field(ge=1)
    happy_path_passed_a: int = Field(ge=0)
    happy_path_passed_b: int = Field(ge=0)
    failure_case_passed_a: int = Field(ge=0)
    failure_case_passed_b: int = Field(ge=0)
    pass_rate_a: float = Field(ge=0.0, le=1.0)
    pass_rate_b: float = Field(ge=0.0, le=1.0)
    avg_duration_ms_a: float = Field(ge=0.0)
    avg_duration_ms_b: float = Field(ge=0.0)
    duration_reduction_pct: float
    avg_tool_calls_a: float = Field(ge=0.0)
    avg_tool_calls_b: float = Field(ge=0.0)
    tool_calls_reduction_pct: float
    error_count_a: int = Field(ge=0)
    error_count_b: int = Field(ge=0)
    cost_usd_a: float | None = Field(default=None, ge=0.0)
    cost_usd_b: float | None = Field(default=None, ge=0.0)


@runtime_checkable
class SkillBenchmarkRunnerPort(Protocol):
    """Port running real baseline A vs candidate skill B executions over supplied test fixtures."""

    def execute_fixture_arm(
        self,
        *,
        package: SkillPackageRecord,
        case: SkillTestCaseSpec,
        arm: Literal["A_BASELINE", "B_SKILL"],
    ) -> SkillBenchmarkCaseOutcome:
        """Execute one fixture case deterministically under arm A or arm B."""


@runtime_checkable
class RegisteredToolLookupPort(Protocol):
    """Minimal read-only lookup checking whether a canonical tool ID is registered."""

    def get(self, tool_id: str) -> Any | None:
        """Return registered ToolManifest for ``tool_id``, or None."""


class QaSkillBenchmarkRequest(BaseModel):
    """Request by independent QA verifier to run A/B benchmarks on a Skill candidate."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version: SemVer
    task_id: TaskId | None = None
    run_id: RunId | None = None
    explicit_outcomes: list[SkillBenchmarkCaseOutcome] = Field(default_factory=list, max_length=64)
    qualitative_rubric: dict[str, float] = Field(default_factory=dict, max_length=16)
    require_improvement: bool = True
    summary: str = Field(min_length=3, max_length=1000)
    timestamp: datetime

    @field_validator("skill_id")
    @classmethod
    def _validate_sid(cls, value: str) -> str:
        return validate_snake_id(value, "skill_id")

    @field_validator("summary")
    @classmethod
    def _validate_sum(cls, value: str) -> str:
        return ensure_not_secret(value, "summary")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class SkillBenchmarkRecord(BaseModel):
    """Immutable P10.04 QA benchmark verification record bound to package SHA-256."""

    model_config = ConfigDict(extra="forbid")

    benchmark_id: str
    skill_id: str
    version: SemVer
    package_sha256: Sha256Hex
    qa_actor_id: str
    benchmark_status: BenchmarkStatus
    metrics: SkillBenchmarkMetrics
    case_outcomes: list[SkillBenchmarkCaseOutcome]
    qualitative_rubric: dict[str, float] = Field(default_factory=dict)
    findings: list[str] = Field(default_factory=list)
    evidence_id: EvidenceId | None = None
    summary: str
    recorded_at: datetime
    audit_event_id: EventId


class ProposeSkillActivationRequest(BaseModel):
    """Request by Master/Operator creating a pending skill activation proposal (P10.05)."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version: SemVer
    rationale: str = Field(min_length=5, max_length=1000)
    task_id: TaskId | None = None
    request_human_approval_if_needed: bool = True
    timestamp: datetime

    @field_validator("skill_id")
    @classmethod
    def _validate_sid(cls, value: str) -> str:
        return validate_snake_id(value, "skill_id")

    @field_validator("rationale")
    @classmethod
    def _validate_rat(cls, value: str) -> str:
        return ensure_not_secret(value, "rationale")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class SkillActivationProposalRecord(BaseModel):
    """Persisted Master skill activation proposal tracking Approval Gate status."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    skill_id: str
    version: SemVer
    package_sha256: Sha256Hex
    benchmark_id: str
    proposal_status: SkillProposalStatus
    requires_human_approval: bool
    sensitivity_reasons: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    approval_action: str = "skill.activate"
    approval_resource: str
    approval_request_fingerprint: Sha256Hex | None = None
    rationale: str
    proposed_by: str
    proposed_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    audit_event_id: EventId


class ActivateSkillRequest(BaseModel):
    """Request activating a benchmark-PASSED, proposed Skill into ``skills/approved/``."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=8, max_length=64)
    approval_id: str | None = Field(default=None, min_length=4, max_length=64)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class SkillActivationOutcome(BaseModel):
    """Result after atomically promoting and activating an APPROVED Skill version."""

    model_config = ConfigDict(extra="forbid")

    package: SkillPackageRecord
    proposal: SkillActivationProposalRecord
    active_pointer: SkillActivePointer
    audit_event_id: EventId


class RollbackSkillRequest(BaseModel):
    """Request rolling back an active Skill safely to a prior verified APPROVED version."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    target_version: SemVer | None = None
    mark_current_as: Literal["DEPRECATED", "DISABLED"] = "DEPRECATED"
    reason: str = Field(min_length=3, max_length=500)
    timestamp: datetime

    @field_validator("skill_id")
    @classmethod
    def _validate_sid(cls, value: str) -> str:
        return validate_snake_id(value, "skill_id")

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        return ensure_not_secret(value, "reason")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class SkillRollbackOutcome(BaseModel):
    """Result returned after atomic rollback to an earlier immutable APPROVED version."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    rolled_back_from_version: SemVer
    restored_version: SemVer
    previous_version_status: Literal["DEPRECATED", "DISABLED"]
    active_pointer: SkillActivePointer
    audit_event_id: EventId


class RecordImprovementOutcomeRequest(BaseModel):
    """Request evaluating real baseline vs post-activation Task outcomes for an APPROVED Skill."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version: SemVer
    source_improvement_id: str | None = Field(default=None, min_length=8, max_length=64)
    baseline_task_ids: list[TaskId] = Field(min_length=1, max_length=100)
    post_activation_task_ids: list[TaskId] = Field(min_length=1, max_length=100)
    timestamp: datetime

    @field_validator("skill_id")
    @classmethod
    def _validate_sid(cls, value: str) -> str:
        return validate_snake_id(value, "skill_id")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class SkillImprovementOutcomeRecord(BaseModel):
    """Immutable measured effectiveness comparison before vs after Skill activation."""

    model_config = ConfigDict(extra="forbid")

    outcome_id: str
    skill_id: str
    version: SemVer
    source_improvement_id: str | None = None
    baseline_task_ids: list[TaskId]
    post_activation_task_ids: list[TaskId]
    baseline_error_rate: float = Field(ge=0.0, le=1.0)
    post_error_rate: float = Field(ge=0.0, le=1.0)
    baseline_avg_duration_ms: float | None = Field(default=None, ge=0.0)
    post_avg_duration_ms: float | None = Field(default=None, ge=0.0)
    baseline_avg_tool_calls: float = Field(ge=0.0)
    post_avg_tool_calls: float = Field(ge=0.0)
    regression_detected: bool
    summary: str
    recorded_at: datetime
    audit_event_id: EventId


@runtime_checkable
class SkillLifecycleRepository(Protocol):
    """Persistence contract isolating ``core.skills`` from concrete SQLite tables."""

    def insert_package_atomic(
        self,
        *,
        package: SkillPackageRecord,
        audit_event: Event,
    ) -> SkillPackageRecord:
        """Persist a new Skill package version cleanly inside an atomic transaction."""

    def update_package_atomic(
        self,
        *,
        package: SkillPackageRecord,
        expected_record_version: int,
        audit_event: Event,
    ) -> SkillPackageRecord:
        """Atomically update an existing Skill package version with optimistic locking."""

    def get_package(self, skill_id: str, version: str) -> SkillPackageRecord | None:
        """Fetch one Skill package version."""

    def list_packages(
        self,
        *,
        skill_id: str | None = None,
        lifecycle_status: SkillLifecycleStatus | None = None,
    ) -> list[SkillPackageRecord]:
        """List persisted Skill packages matching optional filters."""

    def get_active_pointer(self, skill_id: str) -> SkillActivePointer | None:
        """Fetch active APPROVED pointer record for ``skill_id``."""

    def list_active_pointers(self) -> list[SkillActivePointer]:
        """List all Skills having an active pointer entry."""

    def insert_it_review_atomic(
        self,
        *,
        review: SkillItReviewRecord,
        updated_package: SkillPackageRecord,
        expected_record_version: int,
        audit_event: Event,
    ) -> tuple[SkillItReviewRecord, SkillPackageRecord]:
        """Persist IT review and move package state atomically."""

    def list_it_reviews(self, skill_id: str, version: str) -> list[SkillItReviewRecord]:
        """List IT technical reviews for a package version."""

    def insert_benchmark_atomic(
        self,
        *,
        benchmark: SkillBenchmarkRecord,
        updated_package: SkillPackageRecord,
        expected_record_version: int,
        audit_event: Event,
    ) -> tuple[SkillBenchmarkRecord, SkillPackageRecord]:
        """Persist immutable QA benchmark and update package benchmark status atomically."""

    def list_benchmarks(self, skill_id: str, version: str) -> list[SkillBenchmarkRecord]:
        """List immutable benchmarks recorded for ``(skill_id, version)``."""

    def insert_activation_proposal_atomic(
        self,
        *,
        proposal: SkillActivationProposalRecord,
        audit_event: Event,
    ) -> SkillActivationProposalRecord:
        """Persist Master skill activation proposal inside an atomic transaction."""

    def get_activation_proposal(self, proposal_id: str) -> SkillActivationProposalRecord | None:
        """Fetch an activation proposal by ID."""

    def list_activation_proposals(
        self,
        *,
        skill_id: str | None = None,
        proposal_status: SkillProposalStatus | None = None,
    ) -> list[SkillActivationProposalRecord]:
        """List activation proposals filtered by Skill or status."""

    def activate_package_atomic(
        self,
        *,
        package: SkillPackageRecord,
        expected_record_version: int,
        proposal: SkillActivationProposalRecord,
        pointer: SkillActivePointer,
        demoted_prior_package: SkillPackageRecord | None,
        audit_event: Event,
    ) -> tuple[SkillPackageRecord, SkillActivationProposalRecord, SkillActivePointer]:
        """Promote package to APPROVED, update pointer, and settle proposal atomically."""

    def rollback_active_pointer_atomic(
        self,
        *,
        previous_active_package: SkillPackageRecord,
        restored_package: SkillPackageRecord,
        pointer: SkillActivePointer,
        audit_event: Event,
    ) -> SkillActivePointer:
        """Atomically revert active pointer to a prior immutable APPROVED version."""

    def insert_improvement_outcome_atomic(
        self,
        *,
        outcome: SkillImprovementOutcomeRecord,
        audit_event: Event,
    ) -> SkillImprovementOutcomeRecord:
        """Persist immutable measured improvement comparison outcome."""

    def list_improvement_outcomes(
        self,
        *,
        skill_id: str | None = None,
    ) -> list[SkillImprovementOutcomeRecord]:
        """List recorded post-activation improvement outcomes."""
