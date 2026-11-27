"""Phase 09 / P09.03 Service & UX Department: brief clarification, user feedback, and deliverable-vs-brief review."""

from __future__ import annotations

from datetime import datetime
import hashlib
import re
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.identifiers import (
    ArtifactId,
    EventId,
    RunId,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
    validate_safe_relative_path,
)
from core.departments._common import (
    DepartmentBehaviorError,
    ToolInvokerPort,
    append_department_event,
    authorize_and_audit,
    event_actor_id,
    invoke_tool_checked,
)
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source

FeedbackKind = Literal[
    "REVISION_REQUESTED",
    "UX_CLARITY_ISSUE",
    "SCOPE_ADJUSTMENT",
    "ACCEPTED_WITH_NOTES",
]
CriterionMatchStatus = Literal["MET", "PARTIAL", "UNMET"]

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_\u00C0-\u024F\u1E00-\u1EFF]{3,}")
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "must",
        "should",
        "include",
        "cần",
        "cho",
        "với",
        "của",
        "các",
        "trong",
        "phải",
    }
)


class ServiceUxValidationError(DepartmentBehaviorError):
    """Raised when Service/UX brief, feedback, or deliverable inspection input is invalid."""


class BriefClarificationRequest(BaseModel):
    """Input brief evaluated by Service/UX for clarity and testable acceptance criteria."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    goal: str = Field(min_length=3, max_length=2000)
    target_audience: str | None = Field(default=None, max_length=300)
    output_format: str | None = Field(default=None, max_length=120)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=32)
    constraints: list[str] = Field(default_factory=list, max_length=32)
    timestamp: datetime

    @field_validator("goal", "target_audience", "output_format")
    @classmethod
    def _validate_text(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "brief_field"))

    @field_validator("acceptance_criteria", "constraints")
    @classmethod
    def _validate_lists(cls, values: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "brief_list")
        return [ensure_not_secret(item, field_name) for item in values]

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class BriefClarificationReport(BaseModel):
    """Structured clarity audit distinguishing clear briefs from ambiguous requests."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    needs_clarification: bool
    missing_fields: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    structured_criteria: list[str] = Field(default_factory=list)
    audit_event_id: EventId


class UserFeedbackRequest(BaseModel):
    """Feedback submitted by human operator or stakeholder for a deliverable or brief."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    artifact_id: ArtifactId | None = None
    feedback_kind: FeedbackKind
    summary: str = Field(min_length=3, max_length=1000)
    actionable_changes: list[str] = Field(default_factory=list, max_length=24)
    timestamp: datetime

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        return ensure_not_secret(value, "summary")

    @field_validator("actionable_changes")
    @classmethod
    def _validate_changes(cls, values: list[str]) -> list[str]:
        return [ensure_not_secret(item, "actionable_changes") for item in values]

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class UserFeedbackRecord(BaseModel):
    """Persisted auditable user feedback item never overriding Master decisions or QA PASS."""

    model_config = ConfigDict(extra="forbid")

    feedback_id: str
    task_id: TaskId
    run_id: RunId | None = None
    artifact_id: ArtifactId | None = None
    feedback_kind: FeedbackKind
    summary: str
    actionable_changes: list[str]
    recorded_by: str
    created_at: datetime
    qa_substitute: Literal[False] = False
    audit_event_id: EventId


class DeliverableComparisonRequest(BaseModel):
    """Compare real deliverable content (inline or workspace file) against brief criteria."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    brief_criteria: list[str] = Field(min_length=1, max_length=32)
    deliverable_relpath: str | None = Field(default=None, max_length=260)
    workspace_root: str | None = Field(default=None, max_length=512)
    deliverable_text: str | None = Field(default=None, max_length=100_000)
    timestamp: datetime

    @field_validator("brief_criteria")
    @classmethod
    def _validate_criteria(cls, values: list[str]) -> list[str]:
        return [ensure_not_secret(item, "brief_criteria") for item in values]

    @field_validator("deliverable_relpath")
    @classmethod
    def _validate_relpath(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_safe_relative_path(value, "deliverable_relpath")

    @field_validator("deliverable_text")
    @classmethod
    def _validate_deliverable_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, "deliverable_text")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class CriterionComparisonItem(BaseModel):
    """Per-criterion match evaluation against observed deliverable content."""

    model_config = ConfigDict(extra="forbid")

    criterion: str
    status: CriterionMatchStatus
    matched_tokens: list[str] = Field(default_factory=list)
    gap_reason: str | None = None


class DeliverableComparisonReport(BaseModel):
    """Objective brief-vs-deliverable UX check strictly separate from QA verification."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    deliverable_path: str | None = None
    deliverable_sha256: Sha256Hex
    criteria_checks: list[CriterionComparisonItem]
    met_count: int = Field(ge=0)
    partial_count: int = Field(ge=0)
    unmet_count: int = Field(ge=0)
    ux_readiness: Literal["READY_FOR_QA_OR_DELIVERY", "NEEDS_REVISION"]
    qa_substitute: Literal[False] = False
    audit_event_id: EventId


def _meaningful_tokens(text: str) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw not in _STOP_WORDS and raw not in seen:
            seen.add(raw)
            tokens.append(raw)
    return tokens


class ServiceUxDepartmentService:
    """Service/UX domain service handling brief clarity, feedback logs, and brief comparisons."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        journal: EventJournal,
        tool_invoker: ToolInvokerPort | None = None,
    ) -> None:
        """Bind Service/UX coordinator to Governance, EventJournal, and optional ToolInvokerPort."""
        self._pm = permission_manager
        self._journal = journal
        self._tools = tool_invoker
        self._feedbacks: list[UserFeedbackRecord] = []

    def clarify_brief(
        self,
        request: BriefClarificationRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> BriefClarificationReport:
        """Inspect request brief for ambiguous goals, audience, format, and acceptance criteria."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.read",
            resource=f"service:clarify:{request.task_id}",
            task_id=request.task_id,
            timestamp=request.timestamp,
            correlation_id=f"svc-clarify-{request.task_id}",
        )
        missing: list[str] = []
        questions: list[str] = []

        if len(request.goal.strip()) < 15:
            missing.append("detailed_goal")
            questions.append("Mục tiêu công việc quá ngắn; vui lòng mô tả rõ đầu ra mong muốn.")
        if not request.target_audience:
            missing.append("target_audience")
            questions.append("Đối tượng người đọc hoặc người sử dụng cuối là ai?")
        if not request.output_format:
            missing.append("output_format")
            questions.append("Định dạng bàn giao mong muốn là Markdown, DOCX, XLSX hay mã nguồn?")
        if not request.acceptance_criteria:
            missing.append("acceptance_criteria")
            questions.append("Vui lòng liệt kê ít nhất một tiêu chí nghiệm thu kiểm tra được.")

        needs_clarification = bool(missing)
        ev = append_department_event(
            self._journal,
            event_type="notification.created",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            actor_id=event_actor_id(auth_context),
            timestamp=request.timestamp,
            correlation_id=f"svc-clarify-{request.task_id}",
            payload={
                "department": "service",
                "operation": "clarify_brief",
                "needs_clarification": needs_clarification,
                "missing_fields": missing,
            },
        )
        return BriefClarificationReport(
            task_id=request.task_id,
            needs_clarification=needs_clarification,
            missing_fields=missing,
            clarification_questions=questions,
            structured_criteria=list(request.acceptance_criteria),
            audit_event_id=ev.event_id,
        )

    def record_user_feedback(
        self,
        request: UserFeedbackRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> UserFeedbackRecord:
        """Record structured stakeholder feedback for revision tracking without bypassing QA."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.read",
            resource=f"service:feedback:{request.task_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=f"svc-fb-{request.task_id}",
        )
        if request.feedback_kind != "ACCEPTED_WITH_NOTES" and not request.actionable_changes:
            raise ServiceUxValidationError(
                "ACTIONABLE_CHANGES_REQUIRED",
                f"Feedback kind '{request.feedback_kind}' requires concrete actionable_changes.",
            )

        feedback_id = f"fb_{uuid.uuid4().hex[:14]}"
        ev = append_department_event(
            self._journal,
            event_type="notification.created",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            run_id=request.run_id,
            artifact_id=request.artifact_id,
            actor_id=event_actor_id(auth_context),
            timestamp=request.timestamp,
            correlation_id=f"svc-fb-{feedback_id}",
            payload={
                "department": "service",
                "operation": "record_user_feedback",
                "feedback_id": feedback_id,
                "feedback_kind": request.feedback_kind,
                "actionable_changes_count": len(request.actionable_changes),
            },
        )
        record = UserFeedbackRecord(
            feedback_id=feedback_id,
            task_id=request.task_id,
            run_id=request.run_id,
            artifact_id=request.artifact_id,
            feedback_kind=request.feedback_kind,
            summary=request.summary,
            actionable_changes=list(request.actionable_changes),
            recorded_by=auth_context.actor_id,
            created_at=request.timestamp,
            audit_event_id=ev.event_id,
        )
        self._feedbacks.append(record)
        return record

    def list_feedback(self, *, task_id: str | None = None) -> list[UserFeedbackRecord]:
        """List recorded user feedback filtered optionally by ``task_id``."""
        if task_id is None:
            return list(self._feedbacks)
        return [f for f in self._feedbacks if f.task_id == task_id]

    def compare_deliverable_against_brief(
        self,
        request: DeliverableComparisonRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> DeliverableComparisonReport:
        """Compare real deliverable content (from workspace or inline) against brief criteria."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="files.read" if request.deliverable_relpath else "task.read",
            resource=f"service:compare:{request.task_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            workspace_root=request.workspace_root if request.deliverable_relpath else None,
            target_path=request.deliverable_relpath,
            timestamp=request.timestamp,
            correlation_id=f"svc-cmp-{request.task_id}",
        )

        content_text = request.deliverable_text
        digest: str | None = None
        if content_text is None:
            if request.deliverable_relpath is None or request.workspace_root is None:
                raise ServiceUxValidationError(
                    "MISSING_DELIVERABLE_SOURCE",
                    "Provide deliverable_text or both deliverable_relpath and workspace_root.",
                )
            read_res = invoke_tool_checked(
                self._tools,
                auth_context=auth_context,
                tool_id="tools.files.read",
                parameters={"path": request.deliverable_relpath},
                task_id=request.task_id,
                run_id=request.run_id,
                workspace_root=request.workspace_root,
                target_path=request.deliverable_relpath,
                timestamp=request.timestamp,
                correlation_id=f"svc-read-{request.task_id}",
            )
            content_text = str(read_res.data.get("content", ""))
            raw_sha = read_res.data.get("sha256")
            digest = str(raw_sha) if isinstance(raw_sha, str) and len(raw_sha) == 64 else None

        if digest is None:
            digest = hashlib.sha256(content_text.encode("utf-8")).hexdigest()

        normalized_body = content_text.lower()
        checks: list[CriterionComparisonItem] = []
        met = 0
        partial = 0
        unmet = 0

        for criterion in request.brief_criteria:
            expected_tokens = _meaningful_tokens(criterion)
            if not expected_tokens:
                unmet += 1
                checks.append(
                    CriterionComparisonItem(
                        criterion=criterion,
                        status="UNMET",
                        gap_reason="Criterion contains no measurable content tokens.",
                    )
                )
                continue
            matched = [tok for tok in expected_tokens if tok in normalized_body]
            coverage = len(matched) / len(expected_tokens)
            if coverage >= 0.7:
                met += 1
                checks.append(
                    CriterionComparisonItem(
                        criterion=criterion,
                        status="MET",
                        matched_tokens=matched,
                    )
                )
            elif coverage > 0.0:
                partial += 1
                checks.append(
                    CriterionComparisonItem(
                        criterion=criterion,
                        status="PARTIAL",
                        matched_tokens=matched,
                        gap_reason="Deliverable covers only part of criterion keywords.",
                    )
                )
            else:
                unmet += 1
                checks.append(
                    CriterionComparisonItem(
                        criterion=criterion,
                        status="UNMET",
                        gap_reason="No matching evidence tokens observed in deliverable.",
                    )
                )

        readiness: Literal["READY_FOR_QA_OR_DELIVERY", "NEEDS_REVISION"] = (
            "READY_FOR_QA_OR_DELIVERY" if unmet == 0 and partial == 0 else "NEEDS_REVISION"
        )
        ev = append_department_event(
            self._journal,
            event_type="notification.created",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            run_id=request.run_id,
            actor_id=event_actor_id(auth_context),
            timestamp=request.timestamp,
            correlation_id=f"svc-cmp-{request.task_id}",
            payload={
                "department": "service",
                "operation": "compare_deliverable_against_brief",
                "deliverable_path": request.deliverable_relpath,
                "deliverable_sha256": digest,
                "met_count": met,
                "partial_count": partial,
                "unmet_count": unmet,
                "ux_readiness": readiness,
            },
        )
        return DeliverableComparisonReport(
            task_id=request.task_id,
            deliverable_path=request.deliverable_relpath,
            deliverable_sha256=digest,
            criteria_checks=checks,
            met_count=met,
            partial_count=partial,
            unmet_count=unmet,
            ux_readiness=readiness,
            audit_event_id=ev.event_id,
        )
