"""Phase 09 / P09.07 Process & Skills Department: post-task retrospective capture and recurring skill proposals."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.identifiers import (
    DepartmentId,
    EventId,
    RunId,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
    validate_snake_id,
)
from core.departments._common import (
    DepartmentBehaviorError,
    append_department_event,
    authorize_and_audit,
    event_actor_id,
)
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source
from core.tools.models import ToolExecutionRecord


class ProcessValidationError(DepartmentBehaviorError):
    """Raised when retrospective records or pattern-detection queries fail validation."""


class TaskRetrospectiveInput(BaseModel):
    """Structured factual summary of tools, errors, fixes, and duration observed on one Task."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    departments_involved: list[DepartmentId] = Field(min_length=1, max_length=9)
    tool_executions: list[ToolExecutionRecord] = Field(default_factory=list)
    errors_observed: list[str] = Field(default_factory=list, max_length=32)
    fixes_applied: list[str] = Field(default_factory=list, max_length=32)
    pattern_tag: str | None = Field(default=None, max_length=64)
    duration_ms: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0.0)
    timestamp: datetime

    @field_validator("errors_observed", "fixes_applied")
    @classmethod
    def _validate_notes(cls, values: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "retrospective_list")
        return [ensure_not_secret(v, field_name) for v in values]

    @field_validator("pattern_tag")
    @classmethod
    def _validate_tag(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_snake_id(value, "pattern_tag")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class ProcessRetrospectiveRecord(BaseModel):
    """Immutable retrospective entry logged after task execution for cross-task analysis."""

    model_config = ConfigDict(extra="forbid")

    retrospective_id: str
    task_id: TaskId
    run_id: RunId | None = None
    departments_involved: list[DepartmentId]
    tools_used: list[str]
    failed_tool_ids: list[str]
    errors_observed: list[str]
    fixes_applied: list[str]
    pattern_tag: str | None = None
    duration_ms: int = Field(ge=0)
    cost_usd: float | None = None
    recorded_at: datetime
    audit_event_id: EventId


class SkillCandidateProposal(BaseModel):
    """Candidate skill draft proposed ONLY from observed repeated patterns (never self-approved)."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    suggested_skill_id: str
    pattern_key: str
    occurrence_count: int = Field(ge=2)
    evidence_task_ids: list[TaskId]
    evidence_retrospective_ids: list[str]
    observed_errors: list[str]
    observed_tools: list[str]
    rationale: str
    status: Literal["PROPOSED_CANDIDATE"] = "PROPOSED_CANDIDATE"
    benchmark_status: Literal["NOT_BENCHMARKED"] = "NOT_BENCHMARKED"
    approved_for_production: Literal[False] = False
    proposed_at: datetime
    audit_event_id: EventId


class ProcessDepartmentService:
    """Process/Skills service capturing real task retrospectives and pattern-backed skill candidates."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        journal: EventJournal,
    ) -> None:
        """Bind Process/Skills service to Governance PermissionManager and EventJournal."""
        self._pm = permission_manager
        self._journal = journal
        self._retrospectives: list[ProcessRetrospectiveRecord] = []
        self._proposals: list[SkillCandidateProposal] = []

    def record_task_retrospective(
        self,
        request: TaskRetrospectiveInput,
        *,
        auth_context: TrustedAuthContext,
    ) -> ProcessRetrospectiveRecord:
        """Record real errors, tool outcomes, fixes, and duration observed during Task execution."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="knowledge.write",
            resource=f"process:retrospective:{request.task_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=f"proc-retro-{request.task_id}",
        )

        tools_used = list(dict.fromkeys(rec.tool_id for rec in request.tool_executions))
        failed_tools = list(
            dict.fromkeys(
                rec.tool_id
                for rec in request.tool_executions
                if rec.status in {"FAILED", "TIMEOUT", "DENIED"}
            )
        )
        observed_errs = list(request.errors_observed)
        for rec in request.tool_executions:
            if rec.status != "SUCCESS" and rec.error_code:
                entry = f"{rec.tool_id}:{rec.error_code}"
                if entry not in observed_errs:
                    observed_errs.append(entry)

        retro_id = f"ret_{uuid.uuid4().hex[:14]}"
        ev = append_department_event(
            self._journal,
            event_type="knowledge.recorded",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            run_id=request.run_id,
            actor_id=event_actor_id(auth_context),
            timestamp=request.timestamp,
            correlation_id=f"proc-retro-{retro_id}",
            payload={
                "department": "process",
                "retrospective_id": retro_id,
                "tools_used": tools_used,
                "failed_tool_ids": failed_tools,
                "errors_count": len(observed_errs),
                "fixes_count": len(request.fixes_applied),
                "pattern_tag": request.pattern_tag,
                "duration_ms": request.duration_ms,
                "cost_usd": request.cost_usd,
            },
        )
        record = ProcessRetrospectiveRecord(
            retrospective_id=retro_id,
            task_id=request.task_id,
            run_id=request.run_id,
            departments_involved=list(request.departments_involved),
            tools_used=tools_used,
            failed_tool_ids=failed_tools,
            errors_observed=observed_errs,
            fixes_applied=list(request.fixes_applied),
            pattern_tag=request.pattern_tag,
            duration_ms=request.duration_ms,
            cost_usd=request.cost_usd,
            recorded_at=request.timestamp,
            audit_event_id=ev.event_id,
        )
        self._retrospectives.append(record)
        return record

    def list_retrospectives(self, *, task_id: str | None = None) -> list[ProcessRetrospectiveRecord]:
        """List stored execution retrospectives optionally filtered by ``task_id``."""
        if task_id is None:
            return list(self._retrospectives)
        return [r for r in self._retrospectives if r.task_id == task_id]

    def detect_patterns_and_propose_skills(
        self,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
        min_occurrences: int = 2,
    ) -> list[SkillCandidateProposal]:
        """Scan accumulated retrospectives and propose candidate skills strictly on repeated patterns."""
        if min_occurrences < 2:
            raise ProcessValidationError(
                "MIN_OCCURRENCES_TOO_LOW",
                "Candidate skill proposal requires at least 2 independent Task observations.",
            )
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="knowledge.write",
            resource="process:pattern_scan",
            timestamp=timestamp,
            correlation_id="proc-pattern-scan",
        )

        grouped: dict[str, list[ProcessRetrospectiveRecord]] = {}
        for item in self._retrospectives:
            key: str | None = None
            if item.pattern_tag is not None:
                key = item.pattern_tag
            elif item.failed_tool_ids:
                key = f"remediate_{item.failed_tool_ids[0].replace('.', '_')}"
            elif item.errors_observed:
                normalized_err = "".join(
                    ch if ch.isalnum() else "_" for ch in item.errors_observed[0].lower()
                ).strip("_")[:48]
                if normalized_err:
                    key = f"fix_{normalized_err}"
            if key is not None:
                grouped.setdefault(key, []).append(item)

        already_proposed = {p.pattern_key for p in self._proposals}
        created: list[SkillCandidateProposal] = []
        for pattern_key in sorted(grouped):
            records = grouped[pattern_key]
            distinct_tasks = list(dict.fromkeys(r.task_id for r in records))
            if len(distinct_tasks) < min_occurrences or pattern_key in already_proposed:
                continue

            suggested_id = validate_snake_id(pattern_key[:64], "suggested_skill_id")
            ev = append_department_event(
                self._journal,
                event_type="knowledge.recorded",
                source=actor_kind_to_event_source(auth_context.actor_kind),
                actor_id=event_actor_id(auth_context),
                timestamp=timestamp,
                correlation_id=f"proc-cand-{suggested_id}",
                payload={
                    "department": "process",
                    "operation": "propose_skill_candidate",
                    "pattern_key": pattern_key,
                    "occurrence_count": len(distinct_tasks),
                    "evidence_task_ids": distinct_tasks,
                    "benchmark_status": "NOT_BENCHMARKED",
                    "approved_for_production": False,
                },
            )
            proposal = SkillCandidateProposal(
                candidate_id=f"cand_{uuid.uuid4().hex[:14]}",
                suggested_skill_id=suggested_id,
                pattern_key=pattern_key,
                occurrence_count=len(distinct_tasks),
                evidence_task_ids=distinct_tasks,
                evidence_retrospective_ids=[r.retrospective_id for r in records],
                observed_errors=list(dict.fromkeys(e for r in records for e in r.errors_observed)),
                observed_tools=list(dict.fromkeys(t for r in records for t in r.tools_used)),
                rationale=(
                    f"Detected recurring pattern '{pattern_key}' across {len(distinct_tasks)} distinct tasks; "
                    "drafted unapproved candidate awaiting Phase 10 IT+QA A/B benchmark and approval."
                ),
                proposed_at=timestamp,
                audit_event_id=ev.event_id,
            )
            self._proposals.append(proposal)
            created.append(proposal)
        return created

    def list_skill_candidates(self) -> list[SkillCandidateProposal]:
        """List unapproved skill candidate proposals derived from real task history."""
        return list(self._proposals)
