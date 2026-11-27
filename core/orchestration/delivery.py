"""Worker Collaboration Conflicts (P06.05) and Verified Master Delivery Summary (P06.06)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from core.contracts.event import Event, sanitize_event_payload
from core.contracts.identifiers import ensure_not_secret, new_event_id, validate_aware_datetime
from core.governance.manager import GovernanceAuditError, PermissionDeniedError, PermissionManager
from core.governance.models import PolicyRequest
from core.governance.workspace import validate_workspace_path
from core.orchestration.handoff_models import WorkerHandoffRepository
from core.orchestration.meeting_models import (
    CollaborationHistoryReport,
    CollaborationRepository,
    DeliveryArtifactSummary,
    HandoffTimelineEntry,
    MasterDeliveryCompileRequest,
    MasterDeliverySummary,
    MeetingNotFoundError,
    MeetingPermissionError,
    MeetingStateError,
    StoredWorkerConflict,
    WorkerConflictCheckRequest,
    WorkerConflictRecord,
    WorkerConflictResolveRequest,
    actor_kind_to_event_source,
    build_trusted_auth,
    new_conflict_id,
    new_delivery_id,
)
from engines.antigravity.verification import compute_sha256_file


class CollaborationDeliveryService:
    """Manages worker file/task conflict arbitration (P06.05) and Master final delivery (P06.06)."""

    def __init__(
        self,
        *,
        collaboration_repo: CollaborationRepository,
        handoff_repo: WorkerHandoffRepository,
        task_repo: Any,
        run_repo: Any,
        metadata_repo: Any,
        permission_manager: PermissionManager,
    ) -> None:
        """Inject explicit persistence ports and Governance manager."""
        self._collab = collaboration_repo
        self._handoffs = handoff_repo
        self._tasks = task_repo
        self._runs = run_repo
        self._metadata = metadata_repo
        self._permissions = permission_manager

    def _authorize(
        self,
        *,
        actor_id: str,
        actor_kind: str,
        action: str,
        resource: str,
        task_id: str,
        run_id: str,
        timestamp: Any,
        correlation_id: str,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        req = PolicyRequest(
            auth_context=build_trusted_auth(actor_id, actor_kind),
            action=action,
            resource=resource,
            task_id=task_id,
            run_id=run_id,
            timestamp=timestamp,
            correlation_id=correlation_id,
            parameters=parameters or {},
        )
        try:
            decision = self._permissions.evaluate_and_audit(req)
        except PermissionDeniedError as exc:
            raise MeetingPermissionError(exc.decision.reason_code, exc.decision.message) from exc
        except GovernanceAuditError as exc:
            raise MeetingPermissionError("GOVERNANCE_AUDIT_FAILED", str(exc)) from exc
        if decision.decision != "ALLOW":
            raise MeetingPermissionError(decision.reason_code, decision.message)

    def build_handoff_collaboration_history(
        self,
        *,
        task_id: str,
        run_id: str | None = None,
    ) -> CollaborationHistoryReport:
        """Return chronological Worker handoff history paired with immutable revisions and conflicts."""
        stored_handoffs = self._handoffs.list_handoffs(task_id=task_id, run_id=run_id)
        timeline = [
            HandoffTimelineEntry(
                handoff=item,
                revisions=self._handoffs.list_revisions(item.handoff.handoff_id),
            )
            for item in stored_handoffs
        ]
        conflicts = [c.conflict for c in self._collab.list_conflicts(task_id=task_id, run_id=run_id)]
        return CollaborationHistoryReport(
            task_id=task_id,
            run_id=run_id,
            handoffs=timeline,
            conflicts=conflicts,
        )

    def detect_file_or_task_conflict(
        self,
        request: WorkerConflictCheckRequest,
    ) -> StoredWorkerConflict | None:
        """Detect concurrent two-worker contention on the same file or step without ACK handoff (P06.05)."""
        validate_aware_datetime(request.timestamp, "timestamp")
        task = self._tasks.get_task(request.task_id)
        run = self._runs.get_run(request.run_id)
        if task is None or run is None or run.task_id != task.task_id:
            raise MeetingNotFoundError("TASK_OR_RUN_INVALID", "Task or Run missing or mismatched.")
        if request.agent_a_id == request.agent_b_id:
            raise MeetingStateError("CONFLICT_REQUIRES_TWO_WORKERS", "Conflict detection requires two distinct workers.")

        norm_a = {PurePosixPath(p.replace("\\", "/")).as_posix() for p in request.paths_a}
        norm_b = {PurePosixPath(p.replace("\\", "/")).as_posix() for p in request.paths_b}
        overlapping = sorted(norm_a & norm_b)
        same_step = request.step_a_id == request.step_b_id
        if not overlapping and not same_step:
            return None

        handoffs = self._handoffs.list_handoffs(task_id=task.task_id, run_id=run.run_id)
        serialized = any(
            h.handoff.status == "ACKNOWLEDGED"
            and {h.handoff.from_agent_id, h.handoff.to_agent_id} == {request.agent_a_id, request.agent_b_id}
            and {h.handoff.source_step_id, h.handoff.target_step_id} == {request.step_a_id, request.step_b_id}
            for h in handoffs
        )
        if serialized:
            return None

        conflict_id = new_conflict_id()
        corr_id = request.correlation_id or f"corr-{conflict_id}"
        conflict_type = (
            "STEP_OUTPUT_CONFLICT"
            if same_step
            else "FILE_WRITE_CONTENTION"
        )
        record = WorkerConflictRecord(
            conflict_id=conflict_id,
            task_id=task.task_id,
            run_id=run.run_id,
            conflict_type=conflict_type,
            conflicting_path=overlapping[0] if overlapping else None,
            step_a_id=request.step_a_id,
            agent_a_id=request.agent_a_id,
            session_a_id=request.session_a_id,
            step_b_id=request.step_b_id,
            agent_b_id=request.agent_b_id,
            session_b_id=request.session_b_id,
            status="OPEN",
            detected_at=request.timestamp,
        )
        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="collaboration.conflict_detected",
            source="orchestrator",
            task_id=task.task_id,
            run_id=run.run_id,
            actor_id="core.master",
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "conflict_id": conflict_id,
                    "conflict_type": conflict_type,
                    "conflicting_path": record.conflicting_path,
                    "agents": [request.agent_a_id, request.agent_b_id],
                }
            ),
        )
        return self._collab.create_conflict_atomic(conflict=record, event=event)

    def resolve_worker_conflict(self, request: WorkerConflictResolveRequest) -> StoredWorkerConflict:
        """Resolve an OPEN conflict strictly via Master DecisionRecord or ACKNOWLEDGED Handoff."""
        validate_aware_datetime(request.timestamp, "timestamp")
        stored = self._collab.get_conflict(request.conflict_id)
        if stored is None:
            raise MeetingNotFoundError("CONFLICT_NOT_FOUND", f"Conflict '{request.conflict_id}' not found.")
        cfl = stored.conflict
        if cfl.status != "OPEN":
            raise MeetingStateError("CONFLICT_ALREADY_RESOLVED", f"Conflict '{cfl.conflict_id}' is '{cfl.status}'.")

        corr_id = request.correlation_id or f"corr-{cfl.conflict_id}"
        self._authorize(
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
            action="meeting.decide",
            resource=f"task:{cfl.task_id}/conflict:{cfl.conflict_id}",
            task_id=cfl.task_id,
            run_id=cfl.run_id,
            timestamp=request.timestamp,
            correlation_id=corr_id,
        )

        if request.decision_id is None and request.handoff_id is None:
            raise MeetingStateError(
                "RESOLUTION_PROOF_REQUIRED",
                "Resolving a conflict requires a Master decision_id or an ACKNOWLEDGED handoff_id.",
            )

        if request.decision_id is not None:
            decisions = self._collab.list_decisions(task_id=cfl.task_id)
            if not any(d.decision_id == request.decision_id for d in decisions):
                raise MeetingNotFoundError("DECISION_NOT_FOUND", f"Decision '{request.decision_id}' not found on task.")
            next_status = "RESOLVED_DECIDED"
        else:
            assert request.handoff_id is not None
            hoff = self._handoffs.get_handoff(request.handoff_id)
            if hoff is None or hoff.handoff.task_id != cfl.task_id or hoff.handoff.status != "ACKNOWLEDGED":
                raise MeetingStateError(
                    "HANDOFF_NOT_ACKNOWLEDGED",
                    f"Handoff '{request.handoff_id}' must exist on task in ACKNOWLEDGED status.",
                )
            next_status = "RESOLVED_SERIALIZED"

        next_cfl = cfl.model_copy(
            update={
                "status": next_status,
                "resolution_summary": ensure_not_secret(request.resolution_summary, "resolution_summary"),
                "decision_id": request.decision_id,
                "handoff_id": request.handoff_id,
                "resolved_at": request.timestamp,
            }
        )
        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="collaboration.conflict_resolved",
            source=actor_kind_to_event_source(request.actor_kind),
            task_id=cfl.task_id,
            run_id=cfl.run_id,
            actor_id=request.actor_id,
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "conflict_id": cfl.conflict_id,
                    "status": next_status,
                    "decision_id": request.decision_id,
                    "handoff_id": request.handoff_id,
                }
            ),
        )
        return self._collab.resolve_conflict_atomic(
            conflict=next_cfl,
            expected_record_version=stored.record_version,
            event=event,
        )

    def compile_master_delivery(self, request: MasterDeliveryCompileRequest) -> MasterDeliverySummary:
        """Assemble final Master Delivery Summary; forces NOT_VERIFIED when QA or disk proof fails (P06.06)."""
        validate_aware_datetime(request.timestamp, "timestamp")
        delivery_id = request.delivery_id or new_delivery_id()
        corr_id = request.correlation_id or f"corr-{delivery_id}"

        self._authorize(
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
            action="master.deliver",
            resource=f"task:{request.task_id}/delivery:{delivery_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=corr_id,
            parameters={"override_qa_unverified": request.override_qa_unverified},
        )

        task = self._tasks.get_task(request.task_id)
        run = self._runs.get_run(request.run_id)
        if task is None or run is None or run.task_id != task.task_id:
            raise MeetingNotFoundError("TASK_OR_RUN_INVALID", "Task or Run missing or mismatched.")

        blockers: list[str] = []
        if not request.artifact_ids:
            blockers.append("No deliverable artifact_ids provided for delivery verification.")

        open_conflicts = [
            c.conflict.conflict_id
            for c in self._collab.list_conflicts(task_id=task.task_id, run_id=run.run_id)
            if c.conflict.status == "OPEN"
        ]
        if open_conflicts:
            blockers.append(f"Unresolved worker conflicts remain open: {open_conflicts}.")

        summaries: list[DeliveryArtifactSummary] = []
        for art_id in request.artifact_ids:
            art = self._metadata.get_artifact(art_id)
            if art is None or art.task_id != task.task_id or art.run_id != run.run_id:
                blockers.append(f"Artifact '{art_id}' missing or does not belong to (task='{task.task_id}', run='{run.run_id}').")
                continue

            ws_res = validate_workspace_path(request.workspace_root, art.relative_path, action="files.read")
            disk_exists = False
            actual_sha: str | None = None
            matched = False
            if not ws_res.allowed or ws_res.canonical_path is None:
                blockers.append(f"Artifact '{art_id}' path violation: {ws_res.message}")
            else:
                disk_path = Path(ws_res.canonical_path)
                disk_exists = disk_path.is_file()
                if not disk_exists:
                    blockers.append(f"Artifact '{art_id}' file missing on disk: '{art.relative_path}'.")
                else:
                    actual_sha = compute_sha256_file(disk_path)
                    matched = art.sha256 is not None and actual_sha == art.sha256
                    if not matched:
                        blockers.append(f"Artifact '{art_id}' disk SHA-256 mismatches recorded digest.")

            if art.verification_status != "VERIFIED_PASS":
                blockers.append(f"Artifact '{art_id}' QA status is '{art.verification_status}', not VERIFIED_PASS.")
            if art.evidence_reference is None:
                blockers.append(f"Artifact '{art_id}' lacks independent QA evidence_reference.")

            summaries.append(
                DeliveryArtifactSummary(
                    artifact_id=art.artifact_id,
                    relative_path=art.relative_path,
                    sha256_recorded=art.sha256,
                    sha256_actual=actual_sha,
                    disk_exists=disk_exists,
                    integrity_matched=matched,
                    verification_status=art.verification_status,
                    evidence_reference=art.evidence_reference,
                    download_ref=f"artifact://{task.task_id}/{art.artifact_id}/{art.relative_path}",
                )
            )

        qa_verified = len(blockers) == 0 and bool(summaries)
        status = "VERIFIED" if qa_verified else "NOT_VERIFIED"
        meetings = self._collab.list_meetings(task_id=task.task_id)
        decisions = self._collab.list_decisions(task_id=task.task_id)
        handoffs = self._handoffs.list_handoffs(task_id=task.task_id, run_id=run.run_id)

        delivery = MasterDeliverySummary(
            delivery_id=delivery_id,
            task_id=task.task_id,
            run_id=run.run_id,
            compiled_by=request.actor_id,
            delivery_verification_status=status,
            qa_verified=qa_verified,
            verification_blockers=blockers,
            artifacts=summaries,
            meeting_ids=[m.meeting.meeting_id for m in meetings],
            decision_ids=[d.decision_id for d in decisions],
            handoff_ids=[h.handoff.handoff_id for h in handoffs],
            open_conflict_ids=open_conflicts,
            known_limitations=list(request.known_limitations),
            summary=ensure_not_secret(request.summary, "delivery.summary"),
            compiled_at=request.timestamp,
        )
        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="master.delivery_compiled",
            source=actor_kind_to_event_source(request.actor_kind),
            task_id=task.task_id,
            run_id=run.run_id,
            actor_id=request.actor_id,
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "delivery_id": delivery_id,
                    "verification_status": status,
                    "qa_verified": qa_verified,
                    "artifact_ids": [a.artifact_id for a in summaries],
                    "blocker_count": len(blockers),
                }
            ),
        )
        return self._collab.save_delivery_atomic(delivery=delivery, event=event)
