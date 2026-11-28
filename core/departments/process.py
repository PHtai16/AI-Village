"""Phase 09 / P09.07 + Phase 10 / P10.01 Process & Skills Department: data-driven Task retrospectives."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
import uuid

from core.agents.hr_models import ApprovedSkillCatalogPort, FailClosedEmptySkillCatalog
from core.contracts.event import Event, sanitize_event_payload
from core.contracts.identifiers import DepartmentId, new_event_id
from core.departments._common import (
    DepartmentBehaviorError,
    append_department_event,
    authorize_and_audit,
    event_actor_id,
)
from core.departments.process_models import (
    CompileTaskRetrospectiveRequest,
    CostStatus,
    DurationSource,
    DuplicateRetrospectiveConflictError,
    ExecutionHistoryReaderPort,
    ImprovementMetrics,
    ImprovementStatus,
    PatternAnalysisOutcome,
    PatternAnalysisRequest,
    PatternCategory,
    ProcessImprovementRepository,
    ProcessOwnershipError,
    ProcessRetrospectiveRecord,
    ProcessRetrospectiveRepository,
    ProcessTaskNotFoundError,
    ProcessValidationError,
    RetrospectiveErrorRecord,
    RetrospectiveFixRecord,
    RetrospectiveProviderUsage,
    RetrospectiveStepRecord,
    RetrospectiveToolSummary,
    SkillCandidateProposal,
    SkillOutcome,
    SkillUsageRecord,
    SkillUsageStatus,
    StaleRetrospectiveVersionError,
    StoredTaskRetrospective,
    SuggestedImprovement,
    TaskExecutionEvidenceBundle,
    TaskExecutionSummary,
    TaskRetrospectiveInput,
    TaskRetrospectiveOutcome,
    TaskRetrospectiveRevision,
    compute_summary_fingerprint,
    new_improvement_id,
    new_retrospective_id,
    scrub_text,
)
from core.events.journal import EventJournal, JournalEntry
from core.events.projections import project_timeline_from_entries
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source
from core.tools.models import ToolExecutionRecord

_TRUSTED_SKILL_SOURCES: frozenset[str] = frozenset(
    {"core_runtime", "orchestrator", "agent_runtime", "tool_runtime"}
)
_TRUSTED_TOOL_EVENT_SOURCES: frozenset[str] = frozenset(
    {"provider", "tool_runtime", "agent_runtime", "engine_adapter"}
)


def _check_worker_task_ownership(
    auth_context: TrustedAuthContext,
    task_id: str | None,
) -> None:
    """Reject worker actors trying to read or modify retrospectives of unassigned Tasks."""
    if auth_context.actor_kind == "worker_agent" and task_id is not None:
        if task_id not in set(auth_context.assigned_task_ids):
            raise ProcessOwnershipError(
                "TASK_OWNERSHIP_DENIED",
                f"Worker '{auth_context.actor_id}' is not assigned to task '{task_id}'.",
            )


def _deduplicate_tool_executions(
    records: Sequence[ToolExecutionRecord],
) -> list[ToolExecutionRecord]:
    """Return tool execution records deduplicated strictly by ``execution_id`` in chronological order."""
    by_id: dict[str, ToolExecutionRecord] = {}
    for rec in records:
        if rec.execution_id not in by_id:
            by_id[rec.execution_id] = rec
    return sorted(by_id.values(), key=lambda r: (r.executed_at, r.execution_id))


def _summarize_tools(
    deduped_tools: list[ToolExecutionRecord],
    journal_entries: Sequence[JournalEntry],
    *,
    task_id: str,
) -> tuple[list[str], list[RetrospectiveToolSummary], int, list[str], list[str], list[str]]:
    """Aggregate factual tool call metrics, retries, failures, and timeline observations."""
    timeline = project_timeline_from_entries(journal_entries, task_id=task_id)
    timeline_tools = list(
        dict.fromkeys(row.tool_name for row in timeline.entries if row.tool_name is not None)
    )

    known_exec_ids = {rec.execution_id for rec in deduped_tools}
    extra_observed_tools: list[str] = []
    for entry in journal_entries:
        ev = entry.event
        if ev.event_type not in {"tool.call_observed", "tool.call_completed", "engine.event_forwarded"}:
            continue
        if ev.source not in _TRUSTED_TOOL_EVENT_SOURCES:
            continue
        payload = ev.redacted_payload
        ev_exec_id = payload.get("execution_id")
        if isinstance(ev_exec_id, str) and ev_exec_id in known_exec_ids:
            continue
        raw_tool = payload.get("tool_name") or payload.get("tool_id")
        if isinstance(raw_tool, str) and raw_tool.strip():
            extra_observed_tools.append(raw_tool.strip())

    grouped: dict[str, list[ToolExecutionRecord]] = {}
    for rec in deduped_tools:
        grouped.setdefault(rec.tool_id, []).append(rec)

    tool_summaries: list[RetrospectiveToolSummary] = []
    failed_tool_ids: list[str] = []
    for tool_id, recs in grouped.items():
        recs_sorted = sorted(recs, key=lambda r: (r.executed_at, r.execution_id))
        retry_count = 0
        saw_failure = False
        for rec in recs_sorted:
            if saw_failure:
                retry_count += 1
            if rec.status in {"FAILED", "TIMEOUT", "CANCELLED"}:
                saw_failure = True

        failed_cnt = sum(1 for r in recs_sorted if r.status in {"FAILED", "TIMEOUT"})
        if failed_cnt > 0 or any(r.status == "DENIED" for r in recs_sorted):
            failed_tool_ids.append(tool_id)
        costs = [r.estimated_cost_usd for r in recs_sorted if r.estimated_cost_usd is not None]
        known_cost = round(sum(costs), 8) if costs else None

        tool_summaries.append(
            RetrospectiveToolSummary(
                tool_id=tool_id,
                invocation_count=len(recs_sorted),
                success_count=sum(1 for r in recs_sorted if r.status == "SUCCESS"),
                failed_count=failed_cnt,
                denied_count=sum(
                    1 for r in recs_sorted if r.status in {"DENIED", "APPROVAL_REQUIRED"}
                ),
                cancelled_count=sum(1 for r in recs_sorted if r.status == "CANCELLED"),
                retry_count=retry_count,
                total_duration_ms=sum(r.duration_ms for r in recs_sorted),
                known_cost_usd=known_cost,
                execution_ids=[r.execution_id for r in recs_sorted],
            )
        )

    for tool_name in dict.fromkeys(extra_observed_tools):
        if tool_name not in grouped:
            cnt = sum(1 for t in extra_observed_tools if t == tool_name)
            tool_summaries.append(
                RetrospectiveToolSummary(
                    tool_id=tool_name,
                    invocation_count=cnt,
                    success_count=cnt,
                    failed_count=0,
                    denied_count=0,
                    cancelled_count=0,
                    retry_count=0,
                    total_duration_ms=0,
                    known_cost_usd=None,
                    execution_ids=[],
                )
            )

    tools_used = list(dict.fromkeys(s.tool_id for s in tool_summaries))
    total_tool_calls = len(deduped_tools) + len(extra_observed_tools)
    exec_ids = [r.execution_id for r in deduped_tools]
    return tools_used, tool_summaries, total_tool_calls, exec_ids, failed_tool_ids, timeline_tools


def _extract_steps(
    bundle: TaskExecutionEvidenceBundle,
    dept_by_agent: dict[str, DepartmentId],
) -> list[RetrospectiveStepRecord]:
    """Assemble factual execution steps strictly from Checklist, WorkerSessions, and Journal."""
    steps: list[RetrospectiveStepRecord] = []
    for item in sorted(bundle.checklist_items, key=lambda c: (c.order, c.checklist_item_id)):
        dur: int | None = None
        if item.created_at is not None and item.completed_at is not None:
            dur = max(0, int((item.completed_at - item.created_at).total_seconds() * 1000))
        dept = dept_by_agent.get(item.assigned_agent_id or "") if item.assigned_agent_id else None
        steps.append(
            RetrospectiveStepRecord(
                checklist_item_id=item.checklist_item_id,
                title=item.title,
                status=item.status,
                agent_id=item.assigned_agent_id,
                department=dept,
                evidence_reference=item.evidence_reference,
                started_at=item.created_at,
                finished_at=item.completed_at,
                duration_ms=dur,
                source="checklist_item",
            )
        )

    for ws in sorted(bundle.worker_steps, key=lambda s: (s.created_at, s.session_id)):
        dur_ws = max(0, int((ws.updated_at - ws.created_at).total_seconds() * 1000))
        steps.append(
            RetrospectiveStepRecord(
                step_id=ws.step_id,
                checklist_item_id=ws.checklist_item_id,
                title=f"Worker step '{ws.step_id}' ({ws.agent_id})",
                status="OBSERVED",
                agent_id=ws.agent_id,
                department=dept_by_agent.get(ws.agent_id),
                session_id=ws.session_id,
                run_id=ws.run_id,
                started_at=ws.created_at,
                finished_at=ws.updated_at,
                duration_ms=dur_ws,
                source="worker_session",
            )
        )

    if not steps:
        for entry in bundle.journal_entries:
            ev = entry.event
            if ev.event_type in {
                "task.created",
                "task.status_changed",
                "run.started",
                "run.completed",
                "run.failed",
                "run.cancelled",
                "run.timed_out",
                "qa.verification_recorded",
            }:
                new_s = ev.redacted_payload.get("new_status") or ev.redacted_payload.get(
                    "verification_status"
                )
                status_label = str(new_s) if new_s is not None else ev.event_type
                steps.append(
                    RetrospectiveStepRecord(
                        step_id=f"evt_{entry.sequence_number}",
                        title=f"{ev.event_type} ({status_label})",
                        status=status_label[:64],
                        agent_id=ev.actor_id,
                        department=dept_by_agent.get(ev.actor_id or ""),
                        session_id=ev.session_id,
                        run_id=ev.run_id,
                        evidence_reference=ev.evidence_id,
                        started_at=ev.timestamp,
                        source="event_journal",
                    )
                )
    return steps


def _extract_errors_and_fixes(
    bundle: TaskExecutionEvidenceBundle,
    sorted_runs: list[Any],
    deduped_tools: list[ToolExecutionRecord],
    extra_errors: list[str],
    extra_fixes: list[str],
    recorded_at: datetime,
) -> tuple[list[RetrospectiveErrorRecord], list[RetrospectiveFixRecord]]:
    """Extract only genuine errors and evidence-backed fixes without LLM fabrication."""
    errors: list[RetrospectiveErrorRecord] = []
    seen_run_errors: set[tuple[str | None, str]] = set()
    seen_exec_errors: set[str] = set()

    for run in sorted_runs:
        if run.error_reference is not None:
            err = run.error_reference
            seen_run_errors.add((run.run_id, err.error_code))
            errors.append(
                RetrospectiveErrorRecord(
                    error_code=err.error_code,
                    message=err.message,
                    stage=err.stage,
                    run_id=run.run_id,
                    source="run_error",
                    occurred_at=run.finished_at or run.started_at,
                )
            )

    for rec in deduped_tools:
        if rec.status != "SUCCESS":
            code = rec.error_code or rec.status
            msg = rec.error_message or f"{rec.tool_id} ended with {rec.status}"
            seen_exec_errors.add(rec.execution_id)
            errors.append(
                RetrospectiveErrorRecord(
                    error_code=code,
                    message=msg,
                    tool_id=rec.tool_id,
                    execution_id=rec.execution_id,
                    run_id=rec.run_id,
                    session_id=rec.session_id,
                    source="tool_execution",
                    occurred_at=rec.executed_at,
                )
            )

    for entry in bundle.journal_entries:
        ev = entry.event
        if ev.event_type in {"runtime.error", "run.failed", "run.timed_out", "tool.call_failed", "tool.call_denied"}:
            payload = ev.redacted_payload
            ex_id = payload.get("execution_id")
            if isinstance(ex_id, str) and ex_id in seen_exec_errors:
                continue
            code = str(payload.get("error_code") or ev.event_type)
            if (ev.run_id, code) in seen_run_errors:
                continue
            msg = str(payload.get("error_message") or payload.get("reason") or ev.event_type)
            errors.append(
                RetrospectiveErrorRecord(
                    error_code=code,
                    message=msg,
                    tool_id=str(payload["tool_id"]) if isinstance(payload.get("tool_id"), str) else None,
                    execution_id=str(ex_id) if isinstance(ex_id, str) else None,
                    run_id=ev.run_id,
                    session_id=ev.session_id,
                    source="event_journal",
                    occurred_at=ev.timestamp,
                )
            )
        elif ev.event_type == "qa.verification_recorded" and ev.source == "qa_verifier":
            ver = str(ev.redacted_payload.get("verification_status", ""))
            if ver == "VERIFIED_FAIL":
                errors.append(
                    RetrospectiveErrorRecord(
                        error_code="QA_VERIFIED_FAIL",
                        message=str(ev.redacted_payload.get("summary") or "Independent QA returned VERIFIED_FAIL."),
                        run_id=ev.run_id,
                        source="qa_verification",
                        occurred_at=ev.timestamp,
                    )
                )

    for note in extra_errors:
        errors.append(
            RetrospectiveErrorRecord(
                error_code="MANUAL_OBSERVED_ERROR",
                message=note,
                source="manual_input",
                occurred_at=recorded_at,
            )
        )

    fixes: list[RetrospectiveFixRecord] = []
    # 1. Tool retry recovery: prior failed/timed-out attempt followed by SUCCESS on same tool_id.
    tools_by_id: dict[str, list[ToolExecutionRecord]] = {}
    for rec in deduped_tools:
        tools_by_id.setdefault(rec.tool_id, []).append(rec)
    for tool_id, recs in tools_by_id.items():
        first_fail: ToolExecutionRecord | None = None
        for rec in recs:
            if rec.status in {"FAILED", "TIMEOUT"} and first_fail is None:
                first_fail = rec
            elif rec.status == "SUCCESS" and first_fail is not None:
                err_code = first_fail.error_code or first_fail.status
                fixes.append(
                    RetrospectiveFixRecord(
                        fix_id=f"fix_tool_{rec.execution_id[:20]}",
                        description=(
                            f"Recovered '{tool_id}' on execution '{rec.execution_id}' after prior "
                            f"{err_code} ({first_fail.execution_id})."
                        ),
                        resolved_error_code=err_code,
                        execution_id=rec.execution_id,
                        run_id=rec.run_id,
                        source="tool_retry_recovery",
                        recorded_at=rec.executed_at,
                    )
                )
                first_fail = None

    # 2. Run retry recovery: prior FAILED/TIMED_OUT run followed by SUCCEEDED run on the Task.
    prior_failed_run: Any | None = None
    for run in sorted_runs:
        if run.status in {"FAILED", "TIMED_OUT"}:
            prior_failed_run = run
        elif run.status == "SUCCEEDED" and prior_failed_run is not None:
            err_code = (
                prior_failed_run.error_reference.error_code
                if prior_failed_run.error_reference is not None
                else prior_failed_run.status
            )
            fixes.append(
                RetrospectiveFixRecord(
                    fix_id=f"fix_run_{run.run_id[:20]}",
                    description=(
                        f"Succeeded on run '{run.run_id}' after prior run '{prior_failed_run.run_id}' "
                        f"ended with {err_code}."
                    ),
                    resolved_error_code=err_code,
                    run_id=run.run_id,
                    source="run_retry_recovery",
                    recorded_at=run.finished_at or run.started_at,
                )
            )
            prior_failed_run = None

    # 3. QA re-verification after failure.
    saw_qa_fail = False
    for entry in bundle.journal_entries:
        ev = entry.event
        if ev.event_type == "qa.verification_recorded" and ev.source == "qa_verifier":
            ver = str(ev.redacted_payload.get("verification_status", ""))
            if ver == "VERIFIED_FAIL":
                saw_qa_fail = True
            elif ver == "VERIFIED_PASS" and saw_qa_fail and ev.evidence_id is not None:
                fixes.append(
                    RetrospectiveFixRecord(
                        fix_id=f"fix_qa_{ev.evidence_id[:20]}",
                        description=(
                            f"Passed independent QA re-verification on artifact '{ev.artifact_id or 'task'}' "
                            f"backed by evidence '{ev.evidence_id}'."
                        ),
                        resolved_error_code="QA_VERIFIED_FAIL",
                        evidence_id=ev.evidence_id,
                        artifact_id=ev.artifact_id,
                        run_id=ev.run_id,
                        source="qa_reverification",
                        recorded_at=ev.timestamp,
                    )
                )
                saw_qa_fail = False

    # 4. Resolved worker conflicts.
    for conflict in bundle.resolved_conflicts:
        fixes.append(
            RetrospectiveFixRecord(
                fix_id=f"fix_cfl_{conflict.conflict_id[:20]}",
                description=conflict.resolution_note,
                run_id=conflict.run_id,
                source="conflict_decision",
                recorded_at=conflict.resolved_at,
            )
        )

    for idx, note in enumerate(extra_fixes, start=1):
        fixes.append(
            RetrospectiveFixRecord(
                fix_id=f"fix_man_{idx}_{uuid.uuid4().hex[:8]}",
                description=note,
                source="manual_input",
                recorded_at=recorded_at,
            )
        )

    return errors, fixes


def _extract_time_cost_and_providers(
    bundle: TaskExecutionEvidenceBundle,
    sorted_runs: list[Any],
    deduped_tools: list[ToolExecutionRecord],
) -> tuple[
    datetime | None,
    datetime | None,
    int | None,
    DurationSource,
    list[RetrospectiveProviderUsage],
    float | None,
    CostStatus,
]:
    """Compute honest timestamps, duration, provider links, and cost without converting null to 0."""
    start_candidates: list[datetime] = [r.started_at for r in sorted_runs if r.started_at is not None]
    if not start_candidates and bundle.task.created_at is not None:
        start_candidates.append(bundle.task.created_at)
    if not start_candidates and deduped_tools:
        start_candidates.append(deduped_tools[0].executed_at)
    if not start_candidates and bundle.journal_entries:
        start_candidates.append(bundle.journal_entries[0].event.timestamp)
    started_at = min(start_candidates) if start_candidates else None

    finish_candidates: list[datetime] = [r.finished_at for r in sorted_runs if r.finished_at is not None]
    if not finish_candidates and bundle.task.status in {"COMPLETED", "FAILED", "CANCELLED"}:
        if bundle.task.updated_at is not None:
            finish_candidates.append(bundle.task.updated_at)
    ended_at = max(finish_candidates) if finish_candidates else None

    duration_ms: int | None = None
    duration_source: DurationSource = "UNAVAILABLE"
    runs_with_measured_tel = [
        r.telemetry.total_duration_ms
        for r in sorted_runs
        if r.telemetry is not None
        and r.telemetry.total_duration_ms is not None
        and r.telemetry.telemetry_status == "VERIFIED_MEASURED"
    ]
    if sorted_runs and len(runs_with_measured_tel) == len(sorted_runs):
        duration_ms = sum(runs_with_measured_tel)
        duration_source = "run_telemetry_measured"
    elif sorted_runs and all(r.started_at is not None and r.finished_at is not None for r in sorted_runs):
        duration_ms = sum(
            max(0, int((r.finished_at - r.started_at).total_seconds() * 1000))
            for r in sorted_runs
        )
        duration_source = "timestamps_measured"
    elif started_at is not None and ended_at is not None:
        duration_ms = max(0, int((ended_at - started_at).total_seconds() * 1000))
        duration_source = "timestamps_measured"
    elif deduped_tools:
        duration_ms = sum(r.duration_ms for r in deduped_tools)
        duration_source = "tool_duration_sum"

    providers: list[RetrospectiveProviderUsage] = []
    seen_prov: set[tuple[str, str | None, str | None, str | None]] = set()
    for run in sorted_runs:
        if run.provider_id is not None:
            key = (run.provider_id, run.connection_id, run.model_id, run.run_id)
            if key not in seen_prov:
                seen_prov.add(key)
                providers.append(
                    RetrospectiveProviderUsage(
                        provider_id=run.provider_id,
                        connection_id=run.connection_id,
                        model_id=run.model_id,
                        run_id=run.run_id,
                    )
                )
    for ws in bundle.worker_steps:
        key = (ws.provider_id, ws.connection_id, ws.model_id, ws.run_id)
        if key not in seen_prov:
            seen_prov.add(key)
            providers.append(
                RetrospectiveProviderUsage(
                    provider_id=ws.provider_id,
                    connection_id=ws.connection_id,
                    model_id=ws.model_id,
                    run_id=ws.run_id,
                )
            )

    # Cost aggregation: preserve None strictly when no provider/tool cost is reported.
    verified_run_actuals = [
        r.telemetry.actual_cost_usd
        for r in sorted_runs
        if r.telemetry is not None
        and r.telemetry.actual_cost_usd is not None
        and r.telemetry.telemetry_status == "VERIFIED_MEASURED"
    ]
    if sorted_runs and len(verified_run_actuals) == len(sorted_runs):
        cost_usd: float | None = round(sum(verified_run_actuals), 8)
        cost_status: CostStatus = "VERIFIED_MEASURED"
    else:
        run_est_by_id: dict[str, float] = {}
        for r in sorted_runs:
            if r.telemetry is not None:
                if r.telemetry.actual_cost_usd is not None:
                    run_est_by_id[r.run_id] = float(r.telemetry.actual_cost_usd)
                elif r.telemetry.estimated_cost_usd is not None:
                    run_est_by_id[r.run_id] = float(r.telemetry.estimated_cost_usd)

        tool_costs_without_run = [
            float(t.estimated_cost_usd)
            for t in deduped_tools
            if t.estimated_cost_usd is not None
            and (t.run_id is None or t.run_id not in run_est_by_id)
        ]
        all_known = list(run_est_by_id.values()) + tool_costs_without_run
        if all_known:
            cost_usd = round(sum(all_known), 8)
            cost_status = "ESTIMATED_FROM_TOOLS_OR_RUN"
        else:
            cost_usd = None
            cost_status = "UNAVAILABLE_FROM_PROVIDER"

    return started_at, ended_at, duration_ms, duration_source, providers, cost_usd, cost_status


def _extract_skill_usage(
    bundle: TaskExecutionEvidenceBundle,
    skill_catalog: ApprovedSkillCatalogPort,
) -> tuple[SkillUsageStatus, list[SkillUsageRecord], list[str]]:
    """Differentiate declared, allowed, invoked, and executed skills without manifest inference."""
    records_by_key: dict[tuple[str | None, str], SkillUsageRecord] = {}

    for snap in bundle.agent_snapshots:
        allowed_set = set(snap.skills_allowed)
        declared_set = set(snap.declared_capabilities)
        for cap_id in snap.declared_capabilities:
            in_catalog = skill_catalog.get_approved_skill(cap_id) is not None
            key = (snap.agent_id, cap_id)
            records_by_key[key] = SkillUsageRecord(
                skill_id=cap_id,
                agent_id=snap.agent_id,
                declared_in_capabilities=True,
                allowed_in_manifest=cap_id in allowed_set,
                approved_in_catalog=in_catalog,
                invoked=False,
                participated_in_execution=False,
                outcome="NOT_INVOKED",
                notes="Declared in manifest; no runtime skill invocation event observed.",
            )
        for skill_id in snap.skills_allowed:
            if skill_id in declared_set:
                continue
            in_catalog = skill_catalog.get_approved_skill(skill_id) is not None
            key = (snap.agent_id, skill_id)
            records_by_key[key] = SkillUsageRecord(
                skill_id=skill_id,
                agent_id=snap.agent_id,
                declared_in_capabilities=False,
                allowed_in_manifest=True,
                approved_in_catalog=in_catalog,
                invoked=False,
                participated_in_execution=False,
                outcome="NOT_INVOKED",
                notes="Allowed in manifest; no runtime skill invocation event observed.",
            )

    allowed_by_agent = {s.agent_id: set(s.skills_allowed) for s in bundle.agent_snapshots}
    declared_by_agent = {s.agent_id: set(s.declared_capabilities) for s in bundle.agent_snapshots}

    for entry in bundle.journal_entries:
        ev = entry.event
        if ev.event_type != "skill.usage_observed" or ev.source not in _TRUSTED_SKILL_SOURCES:
            continue
        payload = ev.redacted_payload
        skill_id = payload.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            continue
        skill_id = skill_id.strip()
        actor = ev.actor_id or (
            str(payload["agent_id"]) if isinstance(payload.get("agent_id"), str) else None
        )
        key = (actor, skill_id)
        existing = records_by_key.get(key)
        allowed = (
            existing.allowed_in_manifest
            if existing is not None
            else (actor is not None and skill_id in allowed_by_agent.get(actor, set()))
        )
        declared = (
            existing.declared_in_capabilities
            if existing is not None
            else (actor is not None and skill_id in declared_by_agent.get(actor, set()))
        )
        in_catalog = skill_catalog.get_approved_skill(skill_id) is not None
        raw_outcome = str(payload.get("outcome", "UNVERIFIED")).upper()
        outcome: SkillOutcome = (
            raw_outcome if raw_outcome in {"SUCCEEDED", "FAILED", "UNVERIFIED"} else "UNVERIFIED"  # type: ignore[assignment]
        )
        participated = bool(payload.get("participated", True))
        note = scrub_text(str(payload.get("notes") or "Observed via skill.usage_observed event."))
        records_by_key[key] = SkillUsageRecord(
            skill_id=skill_id,
            agent_id=actor,
            declared_in_capabilities=declared,
            allowed_in_manifest=allowed,
            approved_in_catalog=in_catalog,
            invoked=True,
            participated_in_execution=participated,
            outcome=outcome,
            evidence_event_id=ev.event_id,
            notes=note,
        )

    skill_usages = sorted(records_by_key.values(), key=lambda s: (s.agent_id or "", s.skill_id))
    skills_invoked = list(dict.fromkeys(s.skill_id for s in skill_usages if s.invoked))
    if skills_invoked:
        status: SkillUsageStatus = "OBSERVED"
    elif skill_usages:
        status = "DECLARED_OR_ALLOWED_ONLY_NOT_INVOKED"
    else:
        status = "UNAVAILABLE_NO_SKILL_DATA"
    return status, skill_usages, skills_invoked


class ProcessDepartmentService:
    """Process/Skills service capturing durable data-driven retrospectives and pattern candidates."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        journal: EventJournal,
        history_reader: ExecutionHistoryReaderPort | None = None,
        retrospective_repo: ProcessRetrospectiveRepository | None = None,
        improvement_repo: ProcessImprovementRepository | None = None,
        skill_catalog: ApprovedSkillCatalogPort | None = None,
    ) -> None:
        """Bind Process/Skills service to Governance, Journal, and optional persistence ports."""
        self._pm = permission_manager
        self._journal = journal
        self._repo = retrospective_repo
        self._history_reader: ExecutionHistoryReaderPort | None = (
            history_reader
            if history_reader is not None
            else (
                retrospective_repo
                if isinstance(retrospective_repo, ExecutionHistoryReaderPort)
                else None
            )
        )
        self._improvement_repo: ProcessImprovementRepository | None = (
            improvement_repo
            if improvement_repo is not None
            else (
                retrospective_repo
                if isinstance(retrospective_repo, ProcessImprovementRepository)
                else None
            )
        )
        self._skill_catalog: ApprovedSkillCatalogPort = (
            skill_catalog if skill_catalog is not None else FailClosedEmptySkillCatalog()
        )
        self._retrospectives: list[ProcessRetrospectiveRecord] = []
        self._proposals: list[SkillCandidateProposal] = []
        self._improvements: dict[tuple[PatternCategory, str], SuggestedImprovement] = {}

    def compile_task_retrospective(
        self,
        request: CompileTaskRetrospectiveRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> TaskRetrospectiveOutcome:
        """Synthesize a factual, deduplicated, persistent Task retrospective from real execution data."""
        if self._history_reader is None:
            raise ProcessValidationError(
                "HISTORY_READER_UNAVAILABLE",
                "ExecutionHistoryReaderPort is required for data-driven retrospective synthesis.",
            )

        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="knowledge.write",
            resource=f"process:retrospective:{request.task_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            workspace_root=request.workspace_root,
            target_path=request.target_path,
            timestamp=request.timestamp,
            correlation_id=f"proc-retro-{request.task_id}",
        )
        _check_worker_task_ownership(auth_context, request.task_id)
        if request.target_path is not None:
            norm_parts = request.target_path.replace("\\", "/").split("/")
            for seg in norm_parts:
                if seg.startswith("task_") and seg != request.task_id:
                    raise ProcessOwnershipError(
                        "CROSS_TASK_WORKSPACE_DENIED",
                        f"Target path '{request.target_path}' references another task '{seg}'.",
                    )

        bundle = self._history_reader.gather_task_execution_bundle(
            request.task_id,
            run_id=request.run_id,
        )
        if bundle is None:
            raise ProcessTaskNotFoundError(
                "TASK_NOT_FOUND",
                f"Task '{request.task_id}' does not exist in persistence storage.",
            )

        sorted_runs = sorted(
            bundle.runs,
            key=lambda r: (
                r.started_at or datetime.min.replace(tzinfo=timezone.utc),
                r.run_id,
            ),
        )
        run_ids = [r.run_id for r in sorted_runs]
        primary_run_id = (
            request.run_id
            or bundle.task.active_run_id
            or (run_ids[-1] if run_ids else None)
        )

        deduped_tools = _deduplicate_tool_executions(
            [*bundle.tool_executions, *request.extra_tool_executions]
        )
        tools_used, tool_summaries, total_tool_calls, exec_ids, failed_tool_ids, timeline_tools = (
            _summarize_tools(deduped_tools, bundle.journal_entries, task_id=bundle.task.task_id)
        )

        dept_by_agent = {s.agent_id: s.department for s in bundle.agent_snapshots}
        session_ids = list(
            dict.fromkeys(
                [
                    *[s.session_id for s in bundle.sessions],
                    *[ws.session_id for ws in bundle.worker_steps],
                    *[r.session_id for r in deduped_tools if r.session_id is not None],
                    *[sid for run in sorted_runs for sid in run.session_ids],
                ]
            )
        )
        agent_ids = list(
            dict.fromkeys(
                [
                    *bundle.task.assigned_agent_ids,
                    *[ws.agent_id for ws in bundle.worker_steps],
                    *[s.agent_id for s in bundle.sessions],
                    *[a.owner_agent_id for a in bundle.artifacts],
                    *[r.actor_id for r in deduped_tools if "." in r.actor_id],
                ]
            )
        )
        departments: list[DepartmentId] = list(
            dict.fromkeys(
                [
                    *[
                        snap.department
                        for snap in bundle.agent_snapshots
                        if snap.department != "orchestration"
                    ],
                    *[art.department for art in bundle.artifacts if art.department is not None],
                ]
            )
        )
        steps = _extract_steps(bundle, dept_by_agent)
        errors_observed, fixes_applied = _extract_errors_and_fixes(
            bundle,
            sorted_runs,
            deduped_tools,
            request.extra_errors,
            request.extra_fixes,
            request.timestamp,
        )
        started_at, ended_at, duration_ms, duration_source, provider_refs, cost_usd, cost_status = (
            _extract_time_cost_and_providers(bundle, sorted_runs, deduped_tools)
        )
        skill_status, skill_usages, skills_invoked = _extract_skill_usage(
            bundle, self._skill_catalog
        )

        tool_art_ids = [
            aid
            for rec in deduped_tools
            for aid in rec.artifact_ids
            if isinstance(aid, str) and aid.startswith("art_")
        ]
        tool_paths = [
            scrub_text(aid)
            for rec in deduped_tools
            for aid in rec.artifact_ids
            if isinstance(aid, str) and not aid.startswith("art_") and aid.strip()
        ]
        artifact_ids = list(
            dict.fromkeys(
                [
                    *[a.artifact_id for a in bundle.artifacts],
                    *bundle.task.artifact_ids,
                    *[aid for run in sorted_runs for aid in run.artifact_ids],
                    *tool_art_ids,
                ]
            )
        )
        output_paths = list(
            dict.fromkeys(
                [
                    *[a.relative_path for a in bundle.artifacts if a.relative_path],
                    *tool_paths,
                ]
            )
        )
        qa_ev_refs: list[str] = []
        qa_statuses: list[str] = [a.verification_status for a in bundle.artifacts]
        for entry in bundle.journal_entries:
            ev = entry.event
            if ev.event_type == "qa.verification_recorded" and ev.source == "qa_verifier":
                if ev.evidence_id is not None:
                    qa_ev_refs.append(ev.evidence_id)
                ver = ev.redacted_payload.get("verification_status")
                if isinstance(ver, str):
                    qa_statuses.append(ver)
        if any(s == "VERIFIED_FAIL" for s in qa_statuses):
            qa_verification_status = "VERIFIED_FAIL"
        elif any(s == "VERIFIED_PASS" for s in qa_statuses):
            qa_verification_status = "VERIFIED_PASS"
        else:
            qa_verification_status = "UNVERIFIED"

        evidence_refs = list(
            dict.fromkeys(
                [
                    *[e.evidence_id for e in bundle.evidence_records],
                    *[a.evidence_reference for a in bundle.artifacts if a.evidence_reference],
                    *[ref for rec in deduped_tools for ref in rec.evidence_refs],
                    *qa_ev_refs,
                ]
            )
        )

        sources_consulted = list(bundle.sources_consulted)
        if request.extra_tool_executions or request.extra_errors or request.extra_fixes:
            if "manual_input" not in sources_consulted:
                sources_consulted.append("manual_input")

        existing = (
            self._repo.get_by_task(request.task_id) if self._repo is not None else None
        )
        canonical_id = existing.retrospective_id if existing is not None else new_retrospective_id()
        next_version = (existing.active_revision + 1) if existing is not None else 1

        fp_material: dict[str, Any] = {
            "task_id": bundle.task.task_id,
            "run_ids": run_ids,
            "session_ids": session_ids,
            "agent_ids": agent_ids,
            "departments_involved": departments,
            "execution_status": bundle.task.status,
            "qa_verification_status": qa_verification_status,
            "started_at": started_at.isoformat() if started_at else None,
            "ended_at": ended_at.isoformat() if ended_at else None,
            "duration_ms": duration_ms,
            "duration_source": duration_source,
            "steps": [s.model_dump(mode="json") for s in steps],
            "tool_summaries": [t.model_dump(mode="json") for t in tool_summaries],
            "total_tool_calls": total_tool_calls,
            "errors_observed": [
                {
                    "error_code": e.error_code,
                    "message": e.message,
                    "tool_id": e.tool_id,
                    "execution_id": e.execution_id,
                    "run_id": e.run_id,
                    "source": e.source,
                }
                for e in errors_observed
            ],
            "fixes_applied": [
                {
                    "description": f.description,
                    "resolved_error_code": f.resolved_error_code,
                    "evidence_id": f.evidence_id,
                    "artifact_id": f.artifact_id,
                    "execution_id": f.execution_id,
                    "run_id": f.run_id,
                    "source": f.source,
                }
                for f in fixes_applied
            ],
            "artifact_ids": artifact_ids,
            "output_paths": output_paths,
            "evidence_refs": evidence_refs,
            "provider_refs": [p.model_dump(mode="json") for p in provider_refs],
            "cost_usd": cost_usd,
            "cost_status": cost_status,
            "skill_usage_status": skill_status,
            "skill_usages": [su.model_dump(mode="json") for su in skill_usages],
            "timeline_observed_tools": timeline_tools,
            "pattern_tag": request.pattern_tag,
        }
        source_fingerprint = compute_summary_fingerprint(fp_material)

        # Short-circuit idempotently before emitting an extra event when already stored identically.
        if existing is not None and existing.source_fingerprint == source_fingerprint:
            return TaskRetrospectiveOutcome(
                stored=existing,
                summary=existing.summary,
                created=False,
                revision_created=False,
                deduplicated=True,
            )

        audit_event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="process.retrospective_recorded",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            run_id=primary_run_id,
            actor_id=event_actor_id(auth_context),
            correlation_id=f"proc-retro-{canonical_id}-v{next_version}",
            redacted_payload=sanitize_event_payload(
                {
                    "department": "process",
                    "retrospective_id": canonical_id,
                    "retrospective_version": next_version,
                    "source_fingerprint": source_fingerprint,
                    "execution_status": bundle.task.status,
                    "tools_used": tools_used,
                    "total_tool_calls": total_tool_calls,
                    "errors_count": len(errors_observed),
                    "fixes_count": len(fixes_applied),
                    "cost_usd": cost_usd,
                    "cost_status": cost_status,
                    "skill_usage_status": skill_status,
                }
            ),
        )

        summary = TaskExecutionSummary(
            retrospective_id=canonical_id,
            retrospective_version=next_version,
            task_id=bundle.task.task_id,
            run_id=primary_run_id,
            run_ids=run_ids,
            session_ids=session_ids,
            agent_ids=agent_ids,
            departments_involved=departments,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            duration_source=duration_source,
            execution_status=bundle.task.status,
            qa_verification_status=qa_verification_status,  # type: ignore[arg-type]
            steps=steps,
            tools_used=tools_used,
            tool_summaries=tool_summaries,
            total_tool_calls=total_tool_calls,
            tool_execution_ids=exec_ids,
            failed_tool_ids=failed_tool_ids,
            errors_observed=errors_observed,
            fixes_applied=fixes_applied,
            artifact_ids=artifact_ids,
            output_paths=output_paths,
            evidence_refs=evidence_refs,
            provider_refs=provider_refs,
            cost_usd=cost_usd,
            cost_status=cost_status,
            skill_usage_status=skill_status,
            skill_usages=skill_usages,
            skills_invoked=skills_invoked,
            timeline_observed_tools=timeline_tools,
            sources_consulted=sources_consulted,
            pattern_tag=request.pattern_tag,
            source_fingerprint=source_fingerprint,
            recorded_at=existing.created_at if existing is not None else request.timestamp,
            updated_at=request.timestamp,
            audit_event_id=audit_event.event_id,
        )

        legacy_view = ProcessRetrospectiveRecord(
            retrospective_id=summary.retrospective_id,
            task_id=summary.task_id,
            run_id=summary.run_id,
            departments_involved=summary.departments_involved or ["process"],
            tools_used=summary.tools_used,
            failed_tool_ids=summary.failed_tool_ids,
            errors_observed=[f"{e.error_code}: {e.message}" for e in summary.errors_observed],
            fixes_applied=[f.description for f in summary.fixes_applied],
            pattern_tag=summary.pattern_tag,
            duration_ms=summary.duration_ms if summary.duration_ms is not None else 0,
            cost_usd=summary.cost_usd,
            recorded_at=summary.recorded_at,
            audit_event_id=summary.audit_event_id,
        )

        if self._repo is not None:
            stored, created, rev_created = self._repo.upsert_retrospective_atomic(
                summary=summary,
                legacy_record=legacy_view,
                actor_id=auth_context.actor_id,
                audit_event=audit_event,
                expected_record_version=request.expected_record_version,
            )
            return TaskRetrospectiveOutcome(
                stored=stored,
                summary=stored.summary,
                created=created,
                revision_created=rev_created,
                deduplicated=not rev_created,
            )

        res = self._journal.append(audit_event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Retrospective event append rejected."
            raise ProcessValidationError("AUDIT_EVENT_REJECTED", msg)
        stored_mem = StoredTaskRetrospective(
            retrospective_id=summary.retrospective_id,
            task_id=summary.task_id,
            primary_run_id=summary.run_id,
            execution_status=summary.execution_status,
            active_revision=summary.retrospective_version,
            record_version=1,
            source_fingerprint=summary.source_fingerprint,
            summary=summary,
            legacy_record=legacy_view,
            linked_run_ids=summary.run_ids,
            created_by=auth_context.actor_id,
            created_at=summary.recorded_at,
            updated_by=auth_context.actor_id,
            updated_at=summary.updated_at,
            last_audit_event_id=summary.audit_event_id,
        )
        self._retrospectives.append(legacy_view)
        return TaskRetrospectiveOutcome(
            stored=stored_mem,
            summary=summary,
            created=True,
            revision_created=True,
            deduplicated=False,
        )

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
        _check_worker_task_ownership(auth_context, request.task_id)

        deduped_tools = _deduplicate_tool_executions(request.tool_executions)
        tools_used = list(dict.fromkeys(rec.tool_id for rec in deduped_tools))
        failed_tools = list(
            dict.fromkeys(
                rec.tool_id
                for rec in deduped_tools
                if rec.status in {"FAILED", "TIMEOUT", "DENIED"}
            )
        )
        observed_errs = [scrub_text(e) for e in request.errors_observed]
        for rec in deduped_tools:
            if rec.status != "SUCCESS" and rec.error_code:
                entry = f"{rec.tool_id}:{rec.error_code}"
                if entry not in observed_errs:
                    observed_errs.append(entry)

        clean_fixes = [scrub_text(f) for f in request.fixes_applied]

        if self._repo is not None:
            existing = self._repo.get_by_task(request.task_id)
            retro_id = existing.retrospective_id if existing is not None else new_retrospective_id()
            next_rev = (existing.active_revision + 1) if existing is not None else 1
            fp = compute_summary_fingerprint(
                {
                    "task_id": request.task_id,
                    "run_id": request.run_id,
                    "departments_involved": request.departments_involved,
                    "tools_used": tools_used,
                    "failed_tool_ids": failed_tools,
                    "errors_observed": observed_errs,
                    "fixes_applied": clean_fixes,
                    "pattern_tag": request.pattern_tag,
                    "duration_ms": request.duration_ms,
                    "cost_usd": request.cost_usd,
                }
            )
            if existing is not None and existing.source_fingerprint == fp and existing.legacy_record is not None:
                return existing.legacy_record

            ev = Event(
                event_id=new_event_id(),
                timestamp=request.timestamp,
                event_type="knowledge.recorded",
                source=actor_kind_to_event_source(auth_context.actor_kind),
                task_id=request.task_id,
                run_id=request.run_id,
                actor_id=event_actor_id(auth_context),
                correlation_id=f"proc-retro-{retro_id}",
                redacted_payload=sanitize_event_payload(
                    {
                        "department": "process",
                        "retrospective_id": retro_id,
                        "tools_used": tools_used,
                        "failed_tool_ids": failed_tools,
                        "errors_count": len(observed_errs),
                        "fixes_count": len(clean_fixes),
                        "pattern_tag": request.pattern_tag,
                        "duration_ms": request.duration_ms,
                        "cost_usd": request.cost_usd,
                    }
                ),
            )
            record = ProcessRetrospectiveRecord(
                retrospective_id=retro_id,
                task_id=request.task_id,
                run_id=request.run_id,
                departments_involved=list(request.departments_involved),
                tools_used=tools_used,
                failed_tool_ids=failed_tools,
                errors_observed=observed_errs,
                fixes_applied=clean_fixes,
                pattern_tag=request.pattern_tag,
                duration_ms=request.duration_ms,
                cost_usd=request.cost_usd,
                recorded_at=existing.created_at if existing is not None else request.timestamp,
                audit_event_id=ev.event_id,
            )
            tool_sums, total_calls = _summarize_manual_tools(deduped_tools)
            summary = TaskExecutionSummary(
                retrospective_id=retro_id,
                retrospective_version=next_rev,
                task_id=request.task_id,
                run_id=request.run_id,
                run_ids=[request.run_id] if request.run_id else [],
                session_ids=list(
                    dict.fromkeys(r.session_id for r in deduped_tools if r.session_id is not None)
                ),
                agent_ids=list(dict.fromkeys(r.actor_id for r in deduped_tools if "." in r.actor_id)),
                departments_involved=list(request.departments_involved),
                duration_ms=request.duration_ms,
                duration_source="explicit_input",
                execution_status="RECORDED_MANUAL",
                qa_verification_status="UNVERIFIED",
                tools_used=tools_used,
                tool_summaries=tool_sums,
                total_tool_calls=total_calls,
                tool_execution_ids=[r.execution_id for r in deduped_tools],
                failed_tool_ids=failed_tools,
                errors_observed=[
                    RetrospectiveErrorRecord(
                        error_code="OBSERVED_ERROR",
                        message=err,
                        run_id=request.run_id,
                        source="manual_input",
                        occurred_at=request.timestamp,
                    )
                    for err in observed_errs
                ],
                fixes_applied=[
                    RetrospectiveFixRecord(
                        fix_id=f"fix_man_{idx}_{uuid.uuid4().hex[:8]}",
                        description=fix,
                        run_id=request.run_id,
                        source="manual_input",
                        recorded_at=request.timestamp,
                    )
                    for idx, fix in enumerate(clean_fixes, start=1)
                ],
                artifact_ids=list(
                    dict.fromkeys(
                        aid
                        for r in deduped_tools
                        for aid in r.artifact_ids
                        if isinstance(aid, str) and aid.startswith("art_")
                    )
                ),
                output_paths=list(
                    dict.fromkeys(
                        scrub_text(aid)
                        for r in deduped_tools
                        for aid in r.artifact_ids
                        if isinstance(aid, str) and not aid.startswith("art_") and aid.strip()
                    )
                ),
                evidence_refs=list(
                    dict.fromkeys(ref for r in deduped_tools for ref in r.evidence_refs)
                ),
                cost_usd=request.cost_usd,
                cost_status=(
                    "ESTIMATED_FROM_TOOLS_OR_RUN"
                    if request.cost_usd is not None
                    else "UNAVAILABLE_FROM_PROVIDER"
                ),
                skill_usage_status="UNAVAILABLE_NO_SKILL_DATA",
                sources_consulted=["manual_input"],
                pattern_tag=request.pattern_tag,
                source_fingerprint=fp,
                recorded_at=record.recorded_at,
                updated_at=request.timestamp,
                audit_event_id=ev.event_id,
            )
            stored, _, _ = self._repo.upsert_retrospective_atomic(
                summary=summary,
                legacy_record=record,
                actor_id=auth_context.actor_id,
                audit_event=ev,
            )
            assert stored.legacy_record is not None
            return stored.legacy_record

        retro_id = new_retrospective_id()
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
                "fixes_count": len(clean_fixes),
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
            fixes_applied=clean_fixes,
            pattern_tag=request.pattern_tag,
            duration_ms=request.duration_ms,
            cost_usd=request.cost_usd,
            recorded_at=request.timestamp,
            audit_event_id=ev.event_id,
        )
        self._retrospectives.append(record)
        return record

    def get_task_retrospective(
        self,
        task_id: str,
        *,
        revision: int | None = None,
        auth_context: TrustedAuthContext | None = None,
        timestamp: datetime | None = None,
    ) -> StoredTaskRetrospective | None:
        """Retrieve stored Task retrospective optionally checking Governance read ownership."""
        if auth_context is not None:
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="knowledge.read",
                resource=f"process:retrospective:{task_id}",
                task_id=task_id,
                timestamp=timestamp or datetime.now(timezone.utc),
                correlation_id=f"proc-read-{task_id}",
            )
            _check_worker_task_ownership(auth_context, task_id)
        if self._repo is not None:
            return self._repo.get_by_task(task_id, revision=revision)
        return None

    def get_retrospective_by_id(
        self,
        retrospective_id: str,
        *,
        revision: int | None = None,
        auth_context: TrustedAuthContext | None = None,
        timestamp: datetime | None = None,
    ) -> StoredTaskRetrospective | None:
        """Retrieve stored Task retrospective by canonical ``retrospective_id``."""
        if self._repo is None:
            return None
        stored = self._repo.get_by_retrospective_id(retrospective_id, revision=revision)
        if auth_context is not None:
            target_task = stored.task_id if stored is not None else None
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="knowledge.read",
                resource=f"process:retrospective:{retrospective_id}",
                task_id=target_task,
                timestamp=timestamp or datetime.now(timezone.utc),
                correlation_id=f"proc-read-{retrospective_id}",
            )
            _check_worker_task_ownership(auth_context, target_task)
        return stored

    def get_retrospective_for_run(
        self,
        run_id: str,
        *,
        auth_context: TrustedAuthContext | None = None,
        timestamp: datetime | None = None,
    ) -> StoredTaskRetrospective | None:
        """Retrieve Task retrospective linked to ``run_id`` cleanly across multi-run tasks."""
        if self._repo is None:
            return None
        stored = self._repo.get_by_run(run_id)
        if auth_context is not None:
            target_task = stored.task_id if stored is not None else None
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="knowledge.read",
                resource=f"process:retrospective:run:{run_id}",
                task_id=target_task,
                run_id=run_id,
                timestamp=timestamp or datetime.now(timezone.utc),
                correlation_id=f"proc-read-run-{run_id}",
            )
            _check_worker_task_ownership(auth_context, target_task)
        return stored

    def list_retrospective_records(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        execution_status: str | None = None,
        auth_context: TrustedAuthContext | None = None,
        timestamp: datetime | None = None,
    ) -> list[StoredTaskRetrospective]:
        """List stored ``StoredTaskRetrospective`` records respecting worker task scoping."""
        if auth_context is not None:
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="knowledge.read",
                resource=f"process:retrospectives:{task_id or run_id or 'all'}",
                task_id=task_id,
                run_id=run_id,
                timestamp=timestamp or datetime.now(timezone.utc),
                correlation_id="proc-list-retrospectives",
            )
            _check_worker_task_ownership(auth_context, task_id)
        if self._repo is None:
            return []
        records = self._repo.list_retrospectives(
            task_id=task_id,
            run_id=run_id,
            execution_status=execution_status,
        )
        if auth_context is not None and auth_context.actor_kind == "worker_agent":
            assigned = set(auth_context.assigned_task_ids)
            records = [r for r in records if r.task_id in assigned]
        return records

    def list_retrospective_revisions(
        self,
        retrospective_id: str,
        *,
        auth_context: TrustedAuthContext | None = None,
        timestamp: datetime | None = None,
    ) -> list[TaskRetrospectiveRevision]:
        """List immutable historical revisions for ``retrospective_id``."""
        if self._repo is None:
            return []
        stored = self._repo.get_by_retrospective_id(retrospective_id)
        if auth_context is not None:
            target_task = stored.task_id if stored is not None else None
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="knowledge.read",
                resource=f"process:retrospective:{retrospective_id}:revisions",
                task_id=target_task,
                timestamp=timestamp or datetime.now(timezone.utc),
                correlation_id=f"proc-rev-{retrospective_id}",
            )
            _check_worker_task_ownership(auth_context, target_task)
        return self._repo.list_revisions(retrospective_id)

    def list_retrospectives(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[ProcessRetrospectiveRecord]:
        """List stored execution retrospectives optionally filtered by ``task_id`` or ``run_id``."""
        if self._repo is not None:
            rows = self._repo.list_retrospectives(task_id=task_id, run_id=run_id)
            out: list[ProcessRetrospectiveRecord] = []
            for row in rows:
                if row.legacy_record is not None:
                    out.append(row.legacy_record)
                else:
                    s = row.summary
                    out.append(
                        ProcessRetrospectiveRecord(
                            retrospective_id=s.retrospective_id,
                            task_id=s.task_id,
                            run_id=s.run_id,
                            departments_involved=s.departments_involved or ["process"],
                            tools_used=s.tools_used,
                            failed_tool_ids=s.failed_tool_ids,
                            errors_observed=[f"{e.error_code}: {e.message}" for e in s.errors_observed],
                            fixes_applied=[f.description for f in s.fixes_applied],
                            pattern_tag=s.pattern_tag,
                            duration_ms=s.duration_ms if s.duration_ms is not None else 0,
                            cost_usd=s.cost_usd,
                            recorded_at=s.recorded_at,
                            audit_event_id=s.audit_event_id,
                        )
                    )
            return out
        items = list(self._retrospectives)
        if task_id is not None:
            items = [r for r in items if r.task_id == task_id]
        if run_id is not None:
            items = [r for r in items if r.run_id == run_id]
        return items

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

        from core.contracts.identifiers import validate_snake_id

        grouped: dict[str, list[ProcessRetrospectiveRecord]] = {}
        for item in self.list_retrospectives():
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

    def analyze_patterns_and_suggest_improvements(
        self,
        request: PatternAnalysisRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> PatternAnalysisOutcome:
        """Scan persisted Task retrospectives across Tasks to detect repeated errors, waste, and skill gaps."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="knowledge.write",
            resource=f"process:pattern_scan:{request.department or 'all'}",
            timestamp=request.timestamp,
            correlation_id="proc-p10-02-pattern-scan",
        )
        if auth_context.actor_kind == "worker_agent":
            assigned = set(auth_context.assigned_task_ids)
            if not request.task_ids or any(tid not in assigned for tid in request.task_ids):
                raise ProcessOwnershipError(
                    "TASK_OWNERSHIP_DENIED",
                    "Worker agents cannot run cross-task pattern scans on unassigned Tasks.",
                )

        if self._repo is None:
            raise ProcessValidationError(
                "RETROSPECTIVE_REPO_UNAVAILABLE",
                "ProcessRetrospectiveRepository is required for cross-task P10.02 pattern detection.",
            )

        all_stored = self._repo.list_retrospectives(limit=0)
        requested_task_set = set(request.task_ids)
        filtered: list[StoredTaskRetrospective] = []
        for rec in all_stored:
            if requested_task_set and rec.task_id not in requested_task_set:
                continue
            if request.department is not None and request.department not in rec.summary.departments_involved:
                continue
            filtered.append(rec)

        filtered.sort(key=lambda r: (r.summary.recorded_at, r.task_id))
        scanned_count = len(filtered)
        if scanned_count < request.min_occurrences:
            existing_all = self._list_existing_improvements()
            return PatternAnalysisOutcome(
                scanned_retrospective_count=scanned_count,
                all_improvements=existing_all,
            )

        candidate_specs = _build_pattern_improvement_specs(
            filtered,
            min_occurrences=request.min_occurrences,
            redundant_call_threshold=request.redundant_call_threshold,
            scanned_task_count=scanned_count,
            timestamp=request.timestamp,
            auth_context=auth_context,
        )

        created: list[SuggestedImprovement] = []
        updated: list[SuggestedImprovement] = []
        unchanged: list[SuggestedImprovement] = []
        last_ev_id: str | None = None

        for spec, ev in candidate_specs:
            if self._improvement_repo is not None:
                saved, was_created, was_updated = self._improvement_repo.upsert_improvement_atomic(
                    improvement=spec,
                    actor_id=auth_context.actor_id,
                    audit_event=ev,
                )
            else:
                saved, was_created, was_updated = self._upsert_improvement_in_memory(spec, ev)
            if was_created:
                created.append(saved)
                last_ev_id = saved.audit_event_id
            elif was_updated:
                updated.append(saved)
                last_ev_id = saved.audit_event_id
            else:
                unchanged.append(saved)

        return PatternAnalysisOutcome(
            scanned_retrospective_count=scanned_count,
            created_improvements=created,
            updated_improvements=updated,
            unchanged_improvements=unchanged,
            all_improvements=self._list_existing_improvements(),
            audit_event_id=last_ev_id,
        )

    def _upsert_improvement_in_memory(
        self,
        spec: SuggestedImprovement,
        audit_event: Event,
    ) -> tuple[SuggestedImprovement, bool, bool]:
        key = (spec.pattern_type, spec.pattern_key)
        existing = self._improvements.get(key)
        if existing is not None and existing.source_fingerprint == spec.source_fingerprint:
            return existing, False, False
        self._journal.append(audit_event)
        if existing is None:
            self._improvements[key] = spec
            return spec, True, False
        next_imp = spec.model_copy(
            update={
                "improvement_id": existing.improvement_id,
                "status": existing.status,
                "record_version": existing.record_version + 1,
                "detected_at": existing.detected_at,
            }
        )
        self._improvements[key] = next_imp
        return next_imp, False, True

    def _list_existing_improvements(
        self,
        *,
        pattern_type: PatternCategory | None = None,
        status: ImprovementStatus | None = None,
    ) -> list[SuggestedImprovement]:
        if self._improvement_repo is not None:
            return self._improvement_repo.list_improvements(
                pattern_type=pattern_type,
                status=status,
            )
        items = list(self._improvements.values())
        if pattern_type is not None:
            items = [i for i in items if i.pattern_type == pattern_type]
        if status is not None:
            items = [i for i in items if i.status == status]
        return sorted(items, key=lambda i: (i.detected_at, i.improvement_id))

    def list_suggested_improvements(
        self,
        *,
        pattern_type: PatternCategory | None = None,
        status: ImprovementStatus | None = None,
        auth_context: TrustedAuthContext | None = None,
        timestamp: datetime | None = None,
    ) -> list[SuggestedImprovement]:
        """List persisted multi-task improvement suggestions respecting Governance read policy."""
        if auth_context is not None:
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="knowledge.read",
                resource=f"process:improvements:{pattern_type or 'all'}",
                timestamp=timestamp or datetime.now(timezone.utc),
                correlation_id="proc-list-improvements",
            )
            if auth_context.actor_kind == "worker_agent":
                assigned = set(auth_context.assigned_task_ids)
                return [
                    imp
                    for imp in self._list_existing_improvements(
                        pattern_type=pattern_type, status=status
                    )
                    if all(tid in assigned for tid in imp.evidence_task_ids)
                ]
        return self._list_existing_improvements(pattern_type=pattern_type, status=status)

    def get_suggested_improvement(
        self,
        improvement_id: str,
        *,
        auth_context: TrustedAuthContext | None = None,
        timestamp: datetime | None = None,
    ) -> SuggestedImprovement | None:
        """Retrieve a single persisted ``SuggestedImprovement`` by identifier."""
        item = (
            self._improvement_repo.get_improvement(improvement_id)
            if self._improvement_repo is not None
            else next(
                (i for i in self._improvements.values() if i.improvement_id == improvement_id),
                None,
            )
        )
        if auth_context is not None:
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="knowledge.read",
                resource=f"process:improvement:{improvement_id}",
                timestamp=timestamp or datetime.now(timezone.utc),
                correlation_id=f"proc-get-imp-{improvement_id}",
            )
            if item is not None and auth_context.actor_kind == "worker_agent":
                assigned = set(auth_context.assigned_task_ids)
                if not all(tid in assigned for tid in item.evidence_task_ids):
                    raise ProcessOwnershipError(
                        "TASK_OWNERSHIP_DENIED",
                        f"Worker '{auth_context.actor_id}' lacks ownership for improvement '{improvement_id}'.",
                    )
        return item

    def update_improvement_status(
        self,
        improvement_id: str,
        *,
        status: ImprovementStatus,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> SuggestedImprovement:
        """Transition a ``SuggestedImprovement`` status cleanly with audit tracking."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="knowledge.write",
            resource=f"process:improvement:{improvement_id}:status",
            timestamp=timestamp,
            correlation_id=f"proc-imp-status-{improvement_id}",
        )
        if auth_context.actor_kind == "worker_agent":
            raise ProcessOwnershipError(
                "IMPROVEMENT_MUTATION_DENIED",
                "Worker agents cannot transition process improvement status.",
            )
        if self._improvement_repo is not None:
            return self._improvement_repo.update_improvement_status(
                improvement_id,
                status=status,
                updated_at=timestamp,
            )
        for k, v in list(self._improvements.items()):
            if v.improvement_id == improvement_id:
                upd = v.model_copy(
                    update={"status": status, "record_version": v.record_version + 1, "updated_at": timestamp}
                )
                self._improvements[k] = upd
                return upd
        raise ProcessValidationError(
            "IMPROVEMENT_NOT_FOUND",
            f"Improvement '{improvement_id}' does not exist.",
        )


def _to_snake_key(raw: str, *, fallback: str = "pattern_item", max_len: int = 56) -> str:
    """Normalize arbitrary error/tool text into a valid snake_case identifier without secrets."""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in raw.lower())
    parts = [p for p in cleaned.split("_") if p]
    joined = "_".join(parts)[:max_len].strip("_")
    if len(joined) < 2:
        joined = fallback
    if not joined[0].isalnum():
        joined = f"p_{joined}"[:max_len]
    return joined


def _aggregate_task_metrics(
    retros: list[StoredTaskRetrospective],
    *,
    scanned_task_count: int,
    error_occurrences: int = 0,
    retry_count_total: int = 0,
    redundant_extra_calls: int = 0,
) -> ImprovementMetrics:
    """Compute strictly factual cross-task metrics without fabricating missing durations or costs."""
    durations = [r.summary.duration_ms for r in retros if r.summary.duration_ms is not None]
    costs = [r.summary.cost_usd for r in retros if r.summary.cost_usd is not None]
    total_dur = sum(durations) if durations else None
    avg_dur = round(total_dur / len(durations), 2) if durations and total_dur is not None else None
    total_cost = round(sum(costs), 8) if costs else None
    n_tasks = len(retros)
    return ImprovementMetrics(
        occurrence_count=n_tasks,
        distinct_task_count=n_tasks,
        scanned_task_count=scanned_task_count,
        affected_task_ratio=round(n_tasks / scanned_task_count, 4),
        error_occurrences=error_occurrences,
        retry_count_total=retry_count_total,
        redundant_extra_calls=redundant_extra_calls,
        avg_duration_ms=avg_dur,
        total_duration_ms=total_dur,
        cost_usd_total=total_cost,
    )


def _finalize_improvement_record(
    *,
    pattern_type: PatternCategory,
    pattern_key: str,
    suggested_skill_id: str,
    title: str,
    rationale: str,
    recommended_action: str,
    retros: list[StoredTaskRetrospective],
    observed_error_codes: list[str],
    observed_tools: list[str],
    observed_fixes: list[str],
    metrics: ImprovementMetrics,
    timestamp: datetime,
    auth_context: TrustedAuthContext,
) -> tuple[SuggestedImprovement, Event]:
    """Assemble validated SuggestedImprovement + corresponding process.pattern_detected Event."""
    ordered = sorted(retros, key=lambda r: (r.summary.recorded_at, r.task_id))
    task_ids = [r.task_id for r in ordered]
    retro_ids = [r.retrospective_id for r in ordered]
    run_ids = list(dict.fromkeys(rid for r in ordered for rid in r.summary.run_ids))
    ev_refs = list(dict.fromkeys(ref for r in ordered for ref in r.summary.evidence_refs))
    depts = list(dict.fromkeys(d for r in ordered for d in r.summary.departments_involved))

    fp = compute_summary_fingerprint(
        {
            "pattern_type": pattern_type,
            "pattern_key": pattern_key,
            "evidence_task_ids": task_ids,
            "evidence_retrospective_ids": retro_ids,
            "observed_error_codes": observed_error_codes,
            "observed_tools": observed_tools,
            "metrics": metrics.model_dump(mode="json"),
        }
    )
    imp_id = new_improvement_id()
    ev = Event(
        event_id=new_event_id(),
        timestamp=timestamp,
        event_type="process.pattern_detected",
        source=actor_kind_to_event_source(auth_context.actor_kind),
        actor_id=event_actor_id(auth_context),
        correlation_id=f"proc-pattern-{pattern_key[:32]}",
        redacted_payload=sanitize_event_payload(
            {
                "department": "process",
                "improvement_id": imp_id,
                "pattern_type": pattern_type,
                "pattern_key": pattern_key,
                "suggested_skill_id": suggested_skill_id,
                "occurrence_count": metrics.occurrence_count,
                "evidence_task_ids": task_ids,
                "evidence_retrospective_ids": retro_ids,
                "source_fingerprint": fp,
            }
        ),
    )
    imp = SuggestedImprovement(
        improvement_id=imp_id,
        pattern_type=pattern_type,
        pattern_key=pattern_key,
        suggested_skill_id=suggested_skill_id,
        title=title,
        rationale=rationale,
        recommended_action=recommended_action,
        evidence_task_ids=task_ids,
        evidence_retrospective_ids=retro_ids,
        evidence_run_ids=run_ids,
        evidence_refs=ev_refs,
        observed_error_codes=observed_error_codes,
        observed_tools=observed_tools,
        observed_fixes=observed_fixes,
        departments_involved=depts,
        metrics=metrics,
        status="SUGGESTED",
        source_fingerprint=fp,
        record_version=1,
        detected_at=timestamp,
        updated_at=timestamp,
        audit_event_id=ev.event_id,
    )
    return imp, ev


def _build_pattern_improvement_specs(
    retros: list[StoredTaskRetrospective],
    *,
    min_occurrences: int,
    redundant_call_threshold: int,
    scanned_task_count: int,
    timestamp: datetime,
    auth_context: TrustedAuthContext,
) -> list[tuple[SuggestedImprovement, Event]]:
    """Detect repeated errors, redundant tool operations, and missing skill gaps cleanly."""
    specs: list[tuple[SuggestedImprovement, Event]] = []

    # 1. Repeated Error patterns grouped by (tool_id, error_code) per distinct Task.
    error_groups: dict[str, dict[str, Any]] = {}
    for rec in retros:
        s = rec.summary
        seen_keys_this_task: set[str] = set()
        for err in s.errors_observed:
            tool_part = f"{err.tool_id.split('.')[-1]}_" if err.tool_id else ""
            raw_key = f"err_{tool_part}{err.error_code}"
            pkey = _to_snake_key(raw_key, fallback="err_recurring")
            grp = error_groups.setdefault(
                pkey,
                {
                    "retros": {},
                    "error_count": 0,
                    "codes": set(),
                    "tools": set(),
                    "fixes": [],
                },
            )
            grp["error_count"] += 1
            grp["codes"].add(err.error_code)
            if err.tool_id:
                grp["tools"].add(err.tool_id)
            if rec.task_id not in seen_keys_this_task:
                seen_keys_this_task.add(rec.task_id)
                grp["retros"][rec.task_id] = rec
                for fix in s.fixes_applied:
                    if not fix.resolved_error_code or fix.resolved_error_code == err.error_code:
                        if fix.description not in grp["fixes"]:
                            grp["fixes"].append(fix.description)
        for failed_tool in s.failed_tool_ids:
            if any(e.tool_id == failed_tool for e in s.errors_observed):
                continue
            pkey = _to_snake_key(f"err_{failed_tool}_failed", fallback="err_tool_failed")
            grp = error_groups.setdefault(
                pkey,
                {
                    "retros": {},
                    "error_count": 0,
                    "codes": set(),
                    "tools": set(),
                    "fixes": [],
                },
            )
            grp["error_count"] += 1
            grp["tools"].add(failed_tool)
            grp["retros"][rec.task_id] = rec

    for pkey in sorted(error_groups):
        grp = error_groups[pkey]
        matched_retros: list[StoredTaskRetrospective] = list(grp["retros"].values())
        if len(matched_retros) < min_occurrences:
            continue
        codes = sorted(grp["codes"])
        tools = sorted(grp["tools"])
        skill_id = _to_snake_key(f"prevent_{pkey}", fallback="prevent_recurring_error")
        metrics = _aggregate_task_metrics(
            matched_retros,
            scanned_task_count=scanned_task_count,
            error_occurrences=int(grp["error_count"]),
        )
        specs.append(
            _finalize_improvement_record(
                pattern_type="repeated_error",
                pattern_key=pkey,
                suggested_skill_id=skill_id,
                title=f"Handle recurring failure pattern '{pkey}'",
                rationale=(
                    f"Observed error(s) {codes or [pkey]} across {len(matched_retros)} of "
                    f"{scanned_task_count} scanned Tasks ({grp['error_count']} error events)."
                ),
                recommended_action=(
                    f"Create preflight validation and remediation skill '{skill_id}' covering "
                    f"tools {tools or ['task_workflow']}."
                ),
                retros=matched_retros,
                observed_error_codes=codes,
                observed_tools=tools,
                observed_fixes=list(grp["fixes"]),
                metrics=metrics,
                timestamp=timestamp,
                auth_context=auth_context,
            )
        )

    # 2. Redundant operations (retries or excessive repeated invocations per tool across Tasks).
    redundant_groups: dict[str, dict[str, Any]] = {}
    for rec in retros:
        s = rec.summary
        for ts in s.tool_summaries:
            extra_calls = max(ts.retry_count, max(0, ts.invocation_count - 1))
            if ts.retry_count >= 1 or ts.invocation_count >= redundant_call_threshold:
                pkey = _to_snake_key(f"redundant_{ts.tool_id}", fallback="redundant_tool_ops")
                grp = redundant_groups.setdefault(
                    pkey,
                    {"retros": {}, "retries": 0, "extra_calls": 0, "tools": set()},
                )
                grp["retros"][rec.task_id] = rec
                grp["retries"] += ts.retry_count
                grp["extra_calls"] += extra_calls
                grp["tools"].add(ts.tool_id)

    for pkey in sorted(redundant_groups):
        grp = redundant_groups[pkey]
        matched_retros = list(grp["retros"].values())
        if len(matched_retros) < min_occurrences:
            continue
        tools = sorted(grp["tools"])
        skill_id = _to_snake_key(f"optimize_{pkey}", fallback="optimize_tool_workflow")
        metrics = _aggregate_task_metrics(
            matched_retros,
            scanned_task_count=scanned_task_count,
            retry_count_total=int(grp["retries"]),
            redundant_extra_calls=int(grp["extra_calls"]),
        )
        specs.append(
            _finalize_improvement_record(
                pattern_type="redundant_operations",
                pattern_key=pkey,
                suggested_skill_id=skill_id,
                title=f"Reduce redundant operations for '{', '.join(tools)}'",
                rationale=(
                    f"Observed {grp['extra_calls']} redundant tool calls and {grp['retries']} retries "
                    f"across {len(matched_retros)} Tasks on {tools}."
                ),
                recommended_action=(
                    f"Standardize batched execution checklist '{skill_id}' to reduce repeated calls."
                ),
                retros=matched_retros,
                observed_error_codes=[],
                observed_tools=tools,
                observed_fixes=[],
                metrics=metrics,
                timestamp=timestamp,
                auth_context=auth_context,
            )
        )

    # 3. Missing Skill Gaps across recurring multi-tool / tagged workflows without invoked skills.
    gap_groups: dict[str, dict[str, Any]] = {}
    for rec in retros:
        s = rec.summary
        if s.skills_invoked:
            continue
        if s.pattern_tag:
            pkey = _to_snake_key(f"gap_{s.pattern_tag}", fallback="gap_workflow")
            suggested = _to_snake_key(s.pattern_tag, fallback="standardized_workflow")
        elif len(s.tools_used) >= 2:
            short_tools = "_".join(sorted(t.split(".")[-1] for t in s.tools_used))
            pkey = _to_snake_key(f"gap_{short_tools}", fallback="gap_multi_tool_workflow")
            suggested = _to_snake_key(f"workflow_{short_tools}", fallback="standardized_multi_tool")
        else:
            uninvoked_declared = [
                su.skill_id for su in s.skill_usages if not su.invoked and not su.approved_in_catalog
            ]
            if not uninvoked_declared:
                continue
            skill_tok = "_".join(sorted(set(uninvoked_declared)))
            pkey = _to_snake_key(f"gap_{skill_tok}", fallback="gap_missing_skill")
            suggested = _to_snake_key(skill_tok, fallback="standardized_skill")

        grp = gap_groups.setdefault(
            pkey,
            {"retros": {}, "suggested": suggested, "tools": set(), "codes": set()},
        )
        grp["retros"][rec.task_id] = rec
        grp["tools"].update(s.tools_used)
        for err in s.errors_observed:
            grp["codes"].add(err.error_code)

    for pkey in sorted(gap_groups):
        grp = gap_groups[pkey]
        matched_retros = list(grp["retros"].values())
        if len(matched_retros) < min_occurrences:
            continue
        tools = sorted(grp["tools"])
        codes = sorted(grp["codes"])
        suggested = str(grp["suggested"])
        metrics = _aggregate_task_metrics(matched_retros, scanned_task_count=scanned_task_count)
        specs.append(
            _finalize_improvement_record(
                pattern_type="missing_skill_gap",
                pattern_key=pkey,
                suggested_skill_id=suggested,
                title=f"Build approved Skill '{suggested}' for recurring unassisted workflow",
                rationale=(
                    f"{len(matched_retros)} Tasks repeated workflow '{pkey}' with tools {tools} "
                    "and zero approved Skill invocations observed."
                ),
                recommended_action=(
                    f"Draft candidate Skill '{suggested}' with explicit triggers, inputs/outputs, and QA benchmark."
                ),
                retros=matched_retros,
                observed_error_codes=codes,
                observed_tools=tools,
                observed_fixes=[],
                metrics=metrics,
                timestamp=timestamp,
                auth_context=auth_context,
            )
        )

    return specs


def _summarize_manual_tools(
    deduped_tools: list[ToolExecutionRecord],
) -> tuple[list[RetrospectiveToolSummary], int]:
    """Summarize tool execution metrics from explicit ``TaskRetrospectiveInput`` records."""
    grouped: dict[str, list[ToolExecutionRecord]] = {}
    for rec in deduped_tools:
        grouped.setdefault(rec.tool_id, []).append(rec)
    out: list[RetrospectiveToolSummary] = []
    for tool_id, recs in grouped.items():
        costs = [r.estimated_cost_usd for r in recs if r.estimated_cost_usd is not None]
        out.append(
            RetrospectiveToolSummary(
                tool_id=tool_id,
                invocation_count=len(recs),
                success_count=sum(1 for r in recs if r.status == "SUCCESS"),
                failed_count=sum(1 for r in recs if r.status in {"FAILED", "TIMEOUT"}),
                denied_count=sum(
                    1 for r in recs if r.status in {"DENIED", "APPROVAL_REQUIRED"}
                ),
                cancelled_count=sum(1 for r in recs if r.status == "CANCELLED"),
                retry_count=0,
                total_duration_ms=sum(r.duration_ms for r in recs),
                known_cost_usd=round(sum(costs), 8) if costs else None,
                execution_ids=[r.execution_id for r in recs],
            )
        )
    return out, len(deduped_tools)


__all__ = [
    "CompileTaskRetrospectiveRequest",
    "DepartmentBehaviorError",
    "DuplicateRetrospectiveConflictError",
    "ExecutionHistoryReaderPort",
    "ImprovementMetrics",
    "ImprovementStatus",
    "PatternAnalysisOutcome",
    "PatternAnalysisRequest",
    "PatternCategory",
    "ProcessDepartmentService",
    "ProcessImprovementRepository",
    "ProcessOwnershipError",
    "ProcessRetrospectiveRecord",
    "ProcessRetrospectiveRepository",
    "ProcessTaskNotFoundError",
    "ProcessValidationError",
    "RetrospectiveErrorRecord",
    "RetrospectiveFixRecord",
    "RetrospectiveProviderUsage",
    "RetrospectiveStepRecord",
    "RetrospectiveToolSummary",
    "SkillCandidateProposal",
    "SkillUsageRecord",
    "StaleRetrospectiveVersionError",
    "StoredTaskRetrospective",
    "SuggestedImprovement",
    "TaskExecutionEvidenceBundle",
    "TaskExecutionSummary",
    "TaskRetrospectiveInput",
    "TaskRetrospectiveOutcome",
    "TaskRetrospectiveRevision",
]
