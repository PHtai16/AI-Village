"""Domain contracts, DAG validation, and persistence port for P05.03 Master Planning."""

from __future__ import annotations

from datetime import datetime
import hashlib
import heapq
import json
from pathlib import PurePosixPath
import re
from typing import Annotated, Any, Literal, Protocol, runtime_checkable
import uuid

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    CapabilityId,
    ChecklistItemId,
    EventId,
    Sha256Hex,
    SnakeId,
    TaskId,
    ensure_not_secret,
    new_checklist_item_id,
    reject_secret_text,
    validate_agent_id,
    validate_aware_datetime,
    validate_capability_id,
    validate_checklist_item_id,
    validate_safe_payload,
    validate_safe_relative_path,
    validate_tool_id_syntax,
    validate_unique_id_list,
)
from core.contracts.references import ChecklistItem
from engines.antigravity.diagnostics import redact_secrets

_PLAN_ID_RE = re.compile(r"^plan_[a-z0-9][a-z0-9_-]{3,63}$")
_STEP_ID_RE = re.compile(r"^step_[a-z0-9][a-z0-9_-]{1,47}$")
_FORBIDDEN_WRITE_TOP_DIRS: frozenset[str] = frozenset(
    {
        "core",
        "engines",
        "agents",
        "storage",
        "tools",
        "skills",
        "api",
        "ui",
        "docs",
        "tests",
        ".git",
        ".runtime",
        ".ssh",
        "secrets",
    }
)
_FORBIDDEN_WRITE_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        "agents.md",
        "pyproject.toml",
        "credentials.json",
        "token.json",
        "id_rsa",
        "id_ed25519",
    }
)
_SENSITIVE_EXTENSIONS: tuple[str, ...] = (".pem", ".key", ".cert", ".crt", ".pfx", ".p12")

PlanStatus = Literal[
    "DRAFT",
    "NEEDS_CLARIFICATION",
    "VALIDATED",
    "APPLIED",
    "SUPERSEDED",
    "REJECTED",
]
PlanningOutcomeStatus = Literal[
    "PLAN_READY",
    "NEEDS_CLARIFICATION",
    "BLOCKED_GOVERNANCE",
    "FAILED_VALIDATION",
]
DataResidencyPolicy = Literal["local_only", "cloud_allowed", "strict_approved_only"]
PlanningSource = Literal["deterministic_planner", "engine_assisted", "operator_supplied"]


def new_plan_id() -> str:
    """Generate a unique canonical Master Work Plan identifier (`plan_*`)."""
    return f"plan_{uuid.uuid4().hex[:16]}"


def validate_plan_id(value: str) -> str:
    """Validate canonical `plan_*` identifier format without secrets."""
    cleaned = ensure_not_secret(value, "plan_id")
    if not _PLAN_ID_RE.fullmatch(cleaned):
        raise ValueError(f"plan_id must match '^plan_[a-z0-9][a-z0-9_-]{{3,63}}$', got {value!r}.")
    return cleaned


def validate_step_id(value: str) -> str:
    """Validate canonical `step_*` identifier format without secrets."""
    cleaned = ensure_not_secret(value, "step_id")
    if not _STEP_ID_RE.fullmatch(cleaned):
        raise ValueError(f"step_id must match '^step_[a-z0-9][a-z0-9_-]{{1,47}}$', got {value!r}.")
    return cleaned


PlanId = Annotated[str, AfterValidator(validate_plan_id)]
StepId = Annotated[str, AfterValidator(validate_step_id)]


def _validate_safe_write_relpath(raw_path: str) -> str:
    """Validate planned write path stays workspace-relative and avoids protected code/secrets."""
    rel = validate_safe_relative_path(raw_path, "allowed_write_paths")
    parts = [part.lower() for part in PurePosixPath(rel).parts]
    if not parts:
        raise ValueError("allowed_write_paths entry must not be empty.")
    if parts[0] in _FORBIDDEN_WRITE_TOP_DIRS:
        raise ValueError(
            f"allowed_write_paths forbids writing inside protected directory '{parts[0]}' ({raw_path!r})."
        )
    for seg in parts:
        if (
            seg in _FORBIDDEN_WRITE_FILENAMES
            or seg.startswith(".env")
            or any(seg.endswith(ext) for ext in _SENSITIVE_EXTENSIONS)
        ):
            raise ValueError(
                f"allowed_write_paths forbids modifying sensitive file '{seg}' ({raw_path!r})."
            )
    return rel


def _validate_safe_read_relpath(raw_path: str) -> str:
    """Validate planned read path stays relative and avoids obvious credential filenames."""
    rel = validate_safe_relative_path(raw_path, "allowed_read_paths")
    parts = [part.lower() for part in PurePosixPath(rel).parts]
    for seg in parts:
        if (
            seg in {".env", ".ssh", "secrets", ".runtime", "credentials.json", "token.json", "id_rsa", "id_ed25519"}
            or seg.startswith(".env.")
            or any(seg.endswith(ext) for ext in _SENSITIVE_EXTENSIONS)
        ):
            raise ValueError(
                f"allowed_read_paths forbids accessing sensitive secret or runtime path '{seg}' ({raw_path!r})."
            )
    return rel


class PlanningError(RuntimeError):
    """Base fail-closed exception raised by Master Planning operations."""

    def __init__(self, error_code: str, message: str) -> None:
        cleaned = redact_secrets(message)
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned


class PlanValidationError(PlanningError):
    """Raised when a brief, DAG, path scope, routing config, or LLM plan fails validation."""


class PlanGovernanceDeniedError(PlanningError):
    """Raised when Governance Policy denies a planning or routing operation."""


class PlanApprovalRequiredError(PlanningError):
    """Raised when Governance requires explicit Human Approval before proceeding."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        approval_request_id: str | None = None,
    ) -> None:
        super().__init__(error_code, message)
        self.approval_request_id = approval_request_id


class StalePlanVersionError(PlanningError):
    """Raised on optimistic lock conflict during plan revision or application."""


class PlanNotFoundError(PlanningError):
    """Raised when referencing a nonexistent `plan_id` or Task active plan."""


class StructuredUserBrief(BaseModel):
    """Structured operator intake brief analyzed by Master before team selection."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=8000)
    requested_by: str = Field(min_length=1, max_length=128)
    constraints: list[str] = Field(default_factory=list, max_length=32)
    required_outputs: list[str] = Field(default_factory=list, max_length=32)
    input_references: list[str] = Field(default_factory=list, max_length=64)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=32)
    preferred_engine_id: SnakeId | None = None
    preferred_provider_id: SnakeId | None = None
    preferred_connection_id: SnakeId | None = None
    preferred_model_id: str | None = Field(default=None, min_length=1, max_length=128)
    data_residency_policy: DataResidencyPolicy = "cloud_allowed"
    priority: Literal["LOW", "NORMAL", "HIGH", "URGENT"] = "NORMAL"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", "objective", "requested_by")
    @classmethod
    def _validate_core_text(cls, value: str, info: object) -> str:
        """Reject whitespace-only fields and credential leaks."""
        return ensure_not_secret(value, getattr(info, "field_name", "brief_field"))

    @field_validator("preferred_model_id")
    @classmethod
    def _validate_model_id(cls, value: str | None) -> str | None:
        """Reject secret-bearing or blank model identifiers."""
        if value is None:
            return None
        return ensure_not_secret(value, "preferred_model_id")

    @field_validator("constraints", "required_outputs", "acceptance_criteria")
    @classmethod
    def _validate_text_lists(cls, values: list[str], info: object) -> list[str]:
        """Validate non-blank, non-secret list items."""
        field_name = getattr(info, "field_name", "brief_list")
        return [ensure_not_secret(item, field_name) for item in values]

    @field_validator("input_references")
    @classmethod
    def _validate_input_references(cls, values: list[str]) -> list[str]:
        """Enforce workspace-relative safe input references."""
        return [_validate_safe_read_relpath(item) for item in values]

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject secret keys or raw credential values inside brief metadata."""
        validate_safe_payload(value, "brief.metadata")
        return value


class PlanningClarification(BaseModel):
    """Explicit gap question surfaced when Master lacks information to plan safely."""

    model_config = ConfigDict(extra="forbid")

    missing_field: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=3, max_length=500)
    reason: str = Field(min_length=3, max_length=500)
    blocking: bool = True

    @field_validator("missing_field", "question", "reason")
    @classmethod
    def _validate_fields(cls, value: str, info: object) -> str:
        """Ensure clarification text is non-blank and free of secrets."""
        return ensure_not_secret(value, getattr(info, "field_name", "clarification"))


class PlanRoutingProposal(BaseModel):
    """Policy-checked engine, provider, connection, and residency recommendation."""

    model_config = ConfigDict(extra="forbid")

    engine_id: SnakeId = "antigravity"
    provider_id: SnakeId | None = None
    connection_id: SnakeId | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    data_residency_policy: DataResidencyPolicy = "cloud_allowed"
    routing_reason: str = Field(min_length=3, max_length=500)

    @field_validator("model_id")
    @classmethod
    def _validate_model_id(cls, value: str | None) -> str | None:
        """Ensure optional model_id carries no secret material."""
        if value is None:
            return None
        return ensure_not_secret(value, "routing_proposal.model_id")

    @field_validator("routing_reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        """Reject blank or secret-bearing routing rationale."""
        return ensure_not_secret(value, "routing_proposal.routing_reason")


class PlanStepSpec(BaseModel):
    """Single dependency-aware execution step inside a Master Work Plan."""

    model_config = ConfigDict(extra="forbid")

    step_id: StepId
    order: int = Field(ge=0, le=10000)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    depends_on: list[StepId] = Field(default_factory=list, max_length=50)
    required_capabilities: list[CapabilityId] = Field(min_length=1, max_length=16)
    input_formats: list[SnakeId] = Field(default_factory=list, max_length=16)
    output_formats: list[SnakeId] = Field(default_factory=list, max_length=16)
    expected_inputs: list[str] = Field(default_factory=list, max_length=32)
    expected_outputs: list[str] = Field(min_length=1, max_length=32)
    allowed_read_paths: list[str] = Field(default_factory=list, max_length=32)
    allowed_write_paths: list[str] = Field(default_factory=list, max_length=32)
    required_tools: list[str] = Field(default_factory=list, max_length=32)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    proposed_agent_ids: list[AgentId] = Field(default_factory=list, max_length=10)
    routing_proposal: PlanRoutingProposal | None = None
    checklist_item_id: ChecklistItemId | None = None
    requires_human_approval: bool = False

    @field_validator("title", "description")
    @classmethod
    def _validate_step_text(cls, value: str, info: object) -> str:
        """Ensure step title and description are non-blank and free of secrets."""
        return ensure_not_secret(value, getattr(info, "field_name", "step_field"))

    @field_validator("depends_on")
    @classmethod
    def _validate_depends_on(cls, values: list[str]) -> list[str]:
        """Deduplicate-check upstream step dependencies."""
        return validate_unique_id_list(values, validate_step_id, "depends_on")

    @field_validator("required_capabilities")
    @classmethod
    def _validate_capabilities(cls, values: list[str]) -> list[str]:
        """Validate deduplicated snake_case capability requirements."""
        return validate_unique_id_list(values, validate_capability_id, "required_capabilities")

    @field_validator("expected_inputs", "expected_outputs", "acceptance_criteria")
    @classmethod
    def _validate_io_and_criteria(cls, values: list[str], info: object) -> list[str]:
        """Ensure I/O descriptions and step acceptance criteria contain no secrets."""
        field_name = getattr(info, "field_name", "step_list")
        return [ensure_not_secret(item, field_name) for item in values]

    @field_validator("allowed_read_paths")
    @classmethod
    def _validate_reads(cls, values: list[str]) -> list[str]:
        """Reject path traversal or secret paths in step read scopes."""
        return [_validate_safe_read_relpath(item) for item in values]

    @field_validator("allowed_write_paths")
    @classmethod
    def _validate_writes(cls, values: list[str]) -> list[str]:
        """Reject path traversal or protected system paths in step write scopes."""
        return [_validate_safe_write_relpath(item) for item in values]

    @field_validator("required_tools")
    @classmethod
    def _validate_tools(cls, values: list[str]) -> list[str]:
        """Require canonical `tools.<category>.<name>` syntax without wildcards."""
        return validate_unique_id_list(values, validate_tool_id_syntax, "required_tools")

    @field_validator("proposed_agent_ids")
    @classmethod
    def _validate_proposed_agents(cls, values: list[str]) -> list[str]:
        """Ensure suggested worker agents are unique and never include core.master."""
        validated = validate_unique_id_list(values, validate_agent_id, "proposed_agent_ids")
        if "core.master" in validated:
            raise ValueError("core.master cannot be proposed as a Worker step executor.")
        return validated

    @model_validator(mode="after")
    def _validate_no_self_dependency(self) -> PlanStepSpec:
        """Prevent direct self-referential step dependencies."""
        if self.step_id in self.depends_on:
            raise ValueError(f"Step '{self.step_id}' cannot depend on itself.")
        return self


class PlanCapabilityAssessment(BaseModel):
    """Factual cross-check of a planned capability against active Registry Workers."""

    model_config = ConfigDict(extra="forbid")

    capability_id: CapabilityId
    matching_active_agent_ids: list[AgentId] = Field(default_factory=list)
    status: Literal[
        "COVERED_DECLARED",
        "COVERED_VALIDATED",
        "COVERED_OBSERVED",
        "MISSING_CAPABILITY",
    ]
    notes: str = Field(min_length=1, max_length=500)

    @field_validator("notes")
    @classmethod
    def _validate_notes(cls, value: str) -> str:
        """Reject secret material inside assessment notes."""
        return ensure_not_secret(value, "notes")


class MasterWorkPlan(BaseModel):
    """Authoritative dependency-verified work plan created by Master without starting Runs."""

    model_config = ConfigDict(extra="forbid")

    plan_id: PlanId = Field(default_factory=new_plan_id)
    task_id: TaskId
    plan_revision: int = Field(default=1, ge=1)
    status: PlanStatus = "VALIDATED"
    goal_summary: str = Field(min_length=1, max_length=2000)
    assumptions: list[str] = Field(default_factory=list, max_length=32)
    missing_information: list[PlanningClarification] = Field(default_factory=list, max_length=32)
    steps: list[PlanStepSpec] = Field(default_factory=list, max_length=200)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)
    routing_proposal: PlanRoutingProposal
    capability_assessments: list[PlanCapabilityAssessment] = Field(default_factory=list)
    missing_capabilities: list[CapabilityId] = Field(default_factory=list)
    requires_human_approval: bool = False
    created_by: AgentId = "core.master"
    created_at: datetime
    change_summary: str = Field(
        default="Initial Master work plan",
        min_length=1,
        max_length=500,
    )
    planning_source: PlanningSource = "deterministic_planner"
    engine_provider_id: SnakeId | None = None
    engine_model_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("goal_summary", "change_summary")
    @classmethod
    def _validate_text(cls, value: str, info: object) -> str:
        """Reject blank or secret-bearing descriptive fields."""
        return ensure_not_secret(value, getattr(info, "field_name", "plan_field"))

    @field_validator("engine_model_id")
    @classmethod
    def _validate_engine_model(cls, value: str | None) -> str | None:
        """Reject secret patterns inside optional engine_model_id."""
        if value is None:
            return None
        return ensure_not_secret(value, "engine_model_id")

    @field_validator("assumptions", "acceptance_criteria")
    @classmethod
    def _validate_lists(cls, values: list[str], info: object) -> list[str]:
        """Reject empty entries or secrets inside plan assumptions/criteria."""
        field_name = getattr(info, "field_name", "plan_list")
        return [ensure_not_secret(item, field_name) for item in values]

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "created_at")

    @model_validator(mode="after")
    def _validate_plan_structure_and_dag(self) -> MasterWorkPlan:
        """Enforce structured completeness and acyclic step dependency graph."""
        if self.status != "NEEDS_CLARIFICATION":
            if not self.steps:
                raise ValueError(
                    "MasterWorkPlan requires at least one PlanStepSpec unless status is NEEDS_CLARIFICATION."
                )
            if not self.acceptance_criteria:
                raise ValueError(
                    "MasterWorkPlan requires non-empty acceptance_criteria unless status is NEEDS_CLARIFICATION."
                )

        if not self.steps:
            return self

        step_by_id: dict[str, PlanStepSpec] = {}
        seen_orders: set[int] = set()
        seen_checklist_ids: set[str] = set()
        for step in self.steps:
            if step.step_id in step_by_id:
                raise ValueError(f"Duplicate step_id '{step.step_id}' in MasterWorkPlan.")
            if step.order in seen_orders:
                raise ValueError(f"Duplicate step order '{step.order}' in MasterWorkPlan.")
            if step.checklist_item_id is not None:
                if step.checklist_item_id in seen_checklist_ids:
                    raise ValueError(
                        f"Duplicate checklist_item_id '{step.checklist_item_id}' in MasterWorkPlan."
                    )
                seen_checklist_ids.add(step.checklist_item_id)
            step_by_id[step.step_id] = step
            seen_orders.add(step.order)

        for step in self.steps:
            for dep in step.depends_on:
                if dep not in step_by_id:
                    raise ValueError(
                        f"Step '{step.step_id}' references unknown dependency '{dep}'."
                    )

        # Verify DAG is strictly acyclic using deterministic Kahn's algorithm.
        self._compute_topological_order(step_by_id)
        return self

    @staticmethod
    def _compute_topological_order(step_by_id: dict[str, PlanStepSpec]) -> list[str]:
        """Compute topological execution order or raise ValueError if a cycle exists."""
        in_degree: dict[str, int] = {sid: 0 for sid in step_by_id}
        dependents: dict[str, list[str]] = {sid: [] for sid in step_by_id}
        for sid, step in step_by_id.items():
            for dep in step.depends_on:
                in_degree[sid] += 1
                dependents[dep].append(sid)

        heap: list[tuple[int, str]] = [
            (step_by_id[sid].order, sid) for sid, deg in in_degree.items() if deg == 0
        ]
        heapq.heapify(heap)
        ordered: list[str] = []
        while heap:
            _, current = heapq.heappop(heap)
            ordered.append(current)
            for nxt in dependents[current]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    heapq.heappush(heap, (step_by_id[nxt].order, nxt))

        if len(ordered) != len(step_by_id):
            cyclic = sorted(sid for sid, deg in in_degree.items() if deg > 0)
            raise ValueError(
                f"Step dependencies form a cycle involving step_ids: {cyclic}."
            )
        return ordered

    def topological_order(self) -> list[str]:
        """Return step identifiers in dependency-safe execution order."""
        if not self.steps:
            return []
        return self._compute_topological_order({step.step_id: step for step in self.steps})

    def canonical_json(self) -> str:
        """Return deterministic compact JSON representation."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def compute_sha256(self) -> str:
        """Return 64-char SHA-256 hex digest of canonical plan JSON."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def to_checklist_items(self, *, created_at: datetime) -> list[ChecklistItem]:
        """Map ordered plan steps to canonical PENDING ChecklistItems without execution claims."""
        validate_aware_datetime(created_at, "created_at")
        ordered_steps = sorted(self.steps, key=lambda item: (item.order, item.step_id))
        items: list[ChecklistItem] = []
        for idx, step in enumerate(ordered_steps):
            chk_id = step.checklist_item_id or new_checklist_item_id()
            desc_parts = [step.description]
            desc_parts.append(f"Capabilities: {', '.join(step.required_capabilities)}")
            desc_parts.append(f"Expected outputs: {', '.join(step.expected_outputs)}")
            desc_parts.append(f"Acceptance criteria: {'; '.join(step.acceptance_criteria)}")
            if step.depends_on:
                desc_parts.append(f"Depends on: {', '.join(step.depends_on)}")
            full_desc = " | ".join(desc_parts)
            if len(full_desc) > 4000:
                full_desc = full_desc[:3997] + "..."
            items.append(
                ChecklistItem(
                    checklist_item_id=chk_id,
                    task_id=self.task_id,
                    title=step.title,
                    description=full_desc,
                    assigned_agent_id=None,
                    status="PENDING",
                    order=idx,
                    evidence_reference=None,
                    created_at=created_at,
                    completed_at=None,
                )
            )
        return items


class StoredMasterPlan(BaseModel):
    """Durable SQLite envelope storing active plan snapshot and optimistic lock version."""

    model_config = ConfigDict(extra="forbid")

    plan_id: PlanId
    task_id: TaskId
    active_revision: int = Field(ge=1)
    record_version: int = Field(ge=1)
    status: PlanStatus
    plan: MasterWorkPlan
    plan_sha256: Sha256Hex
    created_by: AgentId
    created_at: datetime
    updated_by: AgentId
    updated_at: datetime
    last_audit_event_id: EventId

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime, info: object) -> datetime:
        """Require explicit timezone-aware timestamps."""
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))


class PlanRevisionRecord(BaseModel):
    """Immutable historical snapshot of one Master Work Plan revision."""

    model_config = ConfigDict(extra="forbid")

    plan_id: PlanId
    task_id: TaskId
    plan_revision: int = Field(ge=1)
    status: PlanStatus
    plan: MasterWorkPlan
    plan_sha256: Sha256Hex
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
        """Require explicit timezone-aware revision timestamp."""
        return validate_aware_datetime(value, "changed_at")


class PlanningOutcome(BaseModel):
    """Structured result returned by MasterPlanningService operations."""

    model_config = ConfigDict(extra="forbid")

    outcome_status: PlanningOutcomeStatus
    task_id: TaskId
    plan: MasterWorkPlan | None = None
    stored_plan: StoredMasterPlan | None = None
    clarifications: list[PlanningClarification] = Field(default_factory=list)
    checklist_preview: list[ChecklistItem] = Field(default_factory=list)
    missing_capabilities: list[CapabilityId] = Field(default_factory=list)
    audit_event_id: EventId | None = None
    governance_reason: str | None = None
    error_detail: str | None = None

    @field_validator("governance_reason", "error_detail")
    @classmethod
    def _scrub_diagnostics(cls, value: str | None) -> str | None:
        """Redact potential credentials from error/diagnostic descriptions."""
        if value is None:
            return None
        cleaned = value.strip()
        reject_secret_text(cleaned, "planning_diagnostic")
        return redact_secrets(cleaned) or None


@runtime_checkable
class PlanRepository(Protocol):
    """Persistence port isolating ``MasterPlanningService`` from SQLite details."""

    def create_plan(
        self,
        record: StoredMasterPlan,
        initial_revision: PlanRevisionRecord,
        audit_event: Event,
    ) -> StoredMasterPlan:
        """Atomically persist initial plan, revision v1, and audit Event."""

    def revise_plan(
        self,
        record: StoredMasterPlan,
        new_revision: PlanRevisionRecord,
        *,
        expected_record_version: int,
        audit_event: Event,
    ) -> StoredMasterPlan:
        """Atomically save revised plan under optimistic locking and append immutable revision."""

    def get_plan(self, plan_id: str) -> StoredMasterPlan | None:
        """Load active stored plan snapshot by ``plan_id`` or return None."""

    def get_active_plan_for_task(self, task_id: str) -> StoredMasterPlan | None:
        """Load most recent plan bound to ``task_id`` or return None."""

    def list_plan_revisions(self, plan_id: str) -> list[PlanRevisionRecord]:
        """Return ascending immutable revision history for ``plan_id``."""

    def apply_plan_to_task(
        self,
        *,
        plan_id: str,
        expected_plan_record_version: int,
        updated_plan: StoredMasterPlan,
        checklist_items: list[ChecklistItem],
        audit_event: Event,
        updated_at: datetime,
    ) -> tuple[StoredMasterPlan, list[ChecklistItem]]:
        """Atomically persist ChecklistItems, update Task links, mark plan APPLIED, and emit Event."""
