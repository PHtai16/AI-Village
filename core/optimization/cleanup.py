"""Pure AST P11.05 Post-Task Safe Resource Cleanup, Truthful Inventory & Process Handoff Service."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any
import uuid

from core.contracts.event import Event, EventType, sanitize_event_payload
from core.contracts.identifiers import new_event_id, scrub_secret_text
from core.departments._common import authorize_and_audit, event_actor_id
from core.departments.process_models import CompileTaskRetrospectiveRequest
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source
from core.optimization.cleanup_models import (
    CleanupConflictError,
    CleanupGovernanceError,
    CleanupInventorySummary,
    CleanupItemRecord,
    CleanupReferenceError,
    CleanupRepository,
    CleanupStateError,
    CleanupStatus,
    CleanupTransactionContext,
    ManagedTempCleanupExecutorPort,
    PostTaskCleanupOutcome,
    PostTaskCleanupRecord,
    PostTaskCleanupRequest,
    ProcessHandoffStatus,
    ProcessLearningHandoffRecord,
    ProcessRetrospectiveCompilerPort,
)

TERMINAL_TASK_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
TERMINAL_RUN_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"})


def _make_cleanup_event(
    *,
    event_type: EventType,
    auth_context: TrustedAuthContext,
    timestamp: datetime,
    correlation_id: str,
    task_id: str,
    run_id: str | None,
    payload: dict[str, Any],
) -> Event:
    return Event(
        event_id=new_event_id(),
        timestamp=timestamp,
        event_type=event_type,
        source=actor_kind_to_event_source(auth_context.actor_kind),
        task_id=task_id,
        run_id=run_id,
        actor_id=event_actor_id(auth_context),
        correlation_id=correlation_id,
        redacted_payload=sanitize_event_payload(payload),
    )


def _compute_cleanup_fingerprint(request: PostTaskCleanupRequest) -> str:
    canonical = {
        "task_id": request.task_id,
        "run_id": request.run_id,
        "temp_targets": [t.model_dump(mode="json") for t in request.temp_targets],
        "preserve_paths": request.preserve_paths,
        "force_partial_on_failed_run": request.force_partial_on_failed_run,
        "deliver_to_process": request.deliver_to_process,
        "note": request.note,
        "metadata": request.metadata,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _new_item_id() -> str:
    return f"clitem_{uuid.uuid4().hex[:16]}"


class PostTaskCleanupService:
    """Safely cleans managed temp files, preserves Vault/Evidence/Shared refs, inventories resources, and notifies Process."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        cleanup_repository: CleanupRepository,
        temp_executor: ManagedTempCleanupExecutorPort,
        retrospective_compiler: ProcessRetrospectiveCompilerPort | None = None,
    ) -> None:
        """Bind cleanup service to Governance, SQLite CleanupRepository, contained TempExecutor, and optional Process compiler."""
        self._pm = permission_manager
        self._repo = cleanup_repository
        self._temp_exec = temp_executor
        self._retro_compiler = retrospective_compiler

    def cleanup_and_handoff(
        self,
        request: PostTaskCleanupRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> PostTaskCleanupOutcome:
        """Execute post-task safe cleanup, inventory compilation, and durable Process handoff atomically."""
        if auth_context.actor_kind not in ("human_operator", "master_agent", "runtime_service"):
            raise CleanupGovernanceError(
                "CLEANUP_PERMISSION_DENIED",
                (
                    f"Actor '{auth_context.actor_id}' ({auth_context.actor_kind}) "
                    "cannot execute post-task cleanup or Process handoffs."
                ),
            )

        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.transition",
            resource=f"resources:cleanup:{request.task_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=f"clnp-{request.cleanup_id}",
        )

        fingerprint = _compute_cleanup_fingerprint(request)

        def _tx_op(tx: CleanupTransactionContext) -> PostTaskCleanupOutcome:
            task = tx.get_task(request.task_id)
            if task is None:
                raise CleanupReferenceError("TASK_NOT_FOUND", f"Task '{request.task_id}' does not exist.")

            run = None
            if request.run_id is not None:
                run = tx.get_run(request.run_id)
                if run is None or run.task_id != request.task_id:
                    raise CleanupReferenceError(
                        "RUN_NOT_FOUND",
                        f"Run '{request.run_id}' does not exist for Task '{request.task_id}'.",
                    )

            if request.idempotency_key is not None:
                existing = tx.find_cleanup_by_idempotency(
                    request.task_id,
                    request.run_id,
                    request.idempotency_key,
                )
                if existing is not None:
                    if existing.request_fingerprint != fingerprint:
                        raise CleanupConflictError(
                            "IDEMPOTENCY_PAYLOAD_MISMATCH",
                            (
                                f"Idempotency key '{request.idempotency_key}' already recorded "
                                "with a different post-task cleanup payload."
                            ),
                        )
                    existing_items = tx.list_cleanup_items(existing.cleanup_id)
                    existing_handoff = (
                        tx.get_handoff_by_id(existing.handoff_id)
                        if existing.handoff_id is not None
                        else None
                    )
                    return PostTaskCleanupOutcome(
                        cleanup=existing,
                        items=existing_items,
                        handoff=existing_handoff,
                        idempotent_replay=True,
                    )

            if task.status not in TERMINAL_TASK_STATES:
                run_is_terminal = (
                    request.force_partial_on_failed_run
                    and run is not None
                    and run.status in TERMINAL_RUN_STATES
                )
                if not run_is_terminal:
                    raise CleanupStateError(
                        "TASK_NOT_TERMINAL",
                        (
                            f"Refusing cleanup on active Task '{task.task_id}' in status '{task.status}'. "
                            "Tasks must reach COMPLETED, FAILED, or CANCELLED before post-task cleanup."
                        ),
                    )

            existing_handoff_for_task = (
                tx.get_handoff_for_task_run(request.task_id, request.run_id)
                if request.deliver_to_process
                else None
            )
            if existing_handoff_for_task is not None:
                raise CleanupConflictError(
                    "DUPLICATE_PROCESS_HANDOFF",
                    (
                        f"Process handoff '{existing_handoff_for_task.handoff_id}' already exists for "
                        f"Task '{request.task_id}' / Run '{request.run_id}'."
                    ),
                )

            refs = tx.collect_protected_refs(request.task_id)
            protected_set = set(refs.protected_paths)
            shared_set = set(refs.shared_paths) | set(request.preserve_paths)

            items: list[CleanupItemRecord] = []
            preserved_refs: list[str] = []

            # 1. Release any lingering reserved/active scheduler execution leases for finished Task/Run.
            released_leases = tx.release_lingering_leases(
                request.task_id,
                run_id=request.run_id,
                released_at=request.timestamp,
                reason=f"Post-task cleanup released lingering lease for terminal status '{task.status}'.",
            )
            for lid in released_leases:
                items.append(
                    CleanupItemRecord(
                        item_id=_new_item_id(),
                        cleanup_id=request.cleanup_id,
                        resource_kind="EXECUTION_LEASE",
                        target_ref=lid,
                        action="RELEASED",
                        reason="Released remaining scheduler execution lease after completion.",
                        created_at=request.timestamp,
                    )
                )

            # 2. Safely inspect & delete temp targets strictly inside managed_temp_root.
            deleted_count = 0
            deleted_bytes = 0
            blocked_count = 0
            for target in request.temp_targets:
                res = self._temp_exec.delete_managed_temp_target(
                    target.path,
                    resource_kind=target.resource_kind,
                    protected_paths=protected_set,
                    shared_paths=shared_set,
                )
                items.append(
                    CleanupItemRecord(
                        item_id=_new_item_id(),
                        cleanup_id=request.cleanup_id,
                        resource_kind=res.resource_kind,
                        target_ref=scrub_secret_text(res.target_ref),
                        action=res.action,
                        reason=scrub_secret_text(res.reason),
                        size_bytes=res.size_bytes,
                        created_at=request.timestamp,
                    )
                )
                if res.action == "DELETED":
                    deleted_count += 1
                    deleted_bytes += res.size_bytes or 0
                elif res.action in ("BLOCKED_UNSAFE_PATH", "BLOCKED_SHARED_REF", "FAILED"):
                    blocked_count += 1

            # 3. Record explicit preservation of Vault Artifacts, Evidence Records, and Resource Ledger entries.
            shared_art_set = set(refs.shared_artifact_ids)
            for aid in refs.artifact_ids:
                preserved_refs.append(aid)
                items.append(
                    CleanupItemRecord(
                        item_id=_new_item_id(),
                        cleanup_id=request.cleanup_id,
                        resource_kind="ARTIFACT_VAULT",
                        target_ref=aid,
                        action="PRESERVED",
                        reason=(
                            "Preserved shared Vault artifact referenced by multiple Tasks."
                            if aid in shared_art_set
                            else "Preserved active Vault deliverable artifact."
                        ),
                        created_at=request.timestamp,
                    )
                )
            for eid in refs.evidence_ids:
                preserved_refs.append(eid)
                items.append(
                    CleanupItemRecord(
                        item_id=_new_item_id(),
                        cleanup_id=request.cleanup_id,
                        resource_kind="EVIDENCE_RECORD",
                        target_ref=eid,
                        action="PRESERVED",
                        reason="Preserved immutable QA/verification evidence record.",
                        created_at=request.timestamp,
                    )
                )
            for rled_id in refs.ledger_entry_ids:
                preserved_refs.append(rled_id)
                items.append(
                    CleanupItemRecord(
                        item_id=_new_item_id(),
                        cleanup_id=request.cleanup_id,
                        resource_kind="LEDGER_ENTRY",
                        target_ref=rled_id,
                        action="PRESERVED",
                        reason="Preserved append-only resource usage ledger record.",
                        created_at=request.timestamp,
                    )
                )

            if blocked_count > 0:
                cleanup_status: CleanupStatus = (
                    "PARTIAL" if (deleted_count > 0 or len(released_leases) > 0) else "BLOCKED"
                )
            elif deleted_count == 0 and len(released_leases) == 0 and len(request.temp_targets) == 0:
                cleanup_status = "COMPLETED"
            else:
                cleanup_status = "COMPLETED"

            res_summary = tx.summarize_task_resources(request.task_id)
            alerts_snap = tx.get_alerts_snapshot(request.task_id)
            inventory = CleanupInventorySummary(
                task_id=request.task_id,
                run_id=request.run_id,
                deleted_temp_count=deleted_count,
                deleted_bytes_total=deleted_bytes,
                released_lease_count=len(released_leases),
                preserved_artifact_count=len(refs.artifact_ids),
                preserved_evidence_count=len(refs.evidence_ids),
                preserved_ledger_count=len(refs.ledger_entry_ids),
                blocked_safety_count=blocked_count,
                resource_summary=res_summary,
                open_alerts_count=int(alerts_snap.get("open_alerts_count", 0)),
                critical_alerts_count=int(alerts_snap.get("critical_alerts_count", 0)),
            )

            if blocked_count > 0:
                tx.append_event(
                    _make_cleanup_event(
                        event_type="resources.cleanup_blocked",
                        auth_context=auth_context,
                        timestamp=request.timestamp,
                        correlation_id=f"clnp-{request.cleanup_id}",
                        task_id=request.task_id,
                        run_id=request.run_id,
                        payload={
                            "department": "resources",
                            "cleanup_id": request.cleanup_id,
                            "blocked_safety_count": blocked_count,
                            "cleanup_status": cleanup_status,
                        },
                    )
                )

            cleanup_ev = tx.append_event(
                _make_cleanup_event(
                    event_type="resources.cleanup_recorded",
                    auth_context=auth_context,
                    timestamp=request.timestamp,
                    correlation_id=f"clnp-{request.cleanup_id}",
                    task_id=request.task_id,
                    run_id=request.run_id,
                    payload={
                        "department": "resources",
                        "cleanup_id": request.cleanup_id,
                        "cleanup_status": cleanup_status,
                        "deleted_temp_count": deleted_count,
                        "deleted_bytes_total": deleted_bytes,
                        "released_lease_count": len(released_leases),
                        "preserved_artifact_count": len(refs.artifact_ids),
                        "preserved_evidence_count": len(refs.evidence_ids),
                        "preserved_ledger_count": len(refs.ledger_entry_ids),
                        "blocked_safety_count": blocked_count,
                    },
                )
            )

            handoff_id = request.handoff_id if request.deliver_to_process else None
            cleanup_rec = PostTaskCleanupRecord(
                cleanup_id=request.cleanup_id,
                task_id=request.task_id,
                run_id=request.run_id,
                task_status_snapshot=task.status,
                cleanup_status=cleanup_status,
                idempotency_key=request.idempotency_key,
                request_fingerprint=fingerprint,
                inventory=inventory,
                preserved_refs=preserved_refs,
                handoff_id=handoff_id,
                audit_event_id=cleanup_ev.event_id,
                performed_by=auth_context.actor_id,
                created_at=request.timestamp,
                updated_at=request.timestamp,
                record_version=1,
            )
            saved_cleanup = tx.insert_cleanup_record(cleanup_rec)
            saved_items = tx.insert_cleanup_items(items)

            saved_handoff: ProcessLearningHandoffRecord | None = None
            if request.deliver_to_process:
                retro_id = tx.get_retrospective_id_for_task(request.task_id)
                if (
                    retro_id is None
                    and self._retro_compiler is not None
                    and auth_context.actor_kind in ("human_operator", "master_agent")
                ):
                    retro_out = self._retro_compiler.compile_task_retrospective(
                        CompileTaskRetrospectiveRequest(
                            task_id=request.task_id,
                            run_id=request.run_id,
                            timestamp=request.timestamp,
                        ),
                        auth_context=auth_context,
                    )
                    retro_id = retro_out.stored.retrospective_id

                h_status: ProcessHandoffStatus = (
                    "DELIVERED_TO_PROCESS" if retro_id is not None else "PENDING_PROCESS_LINK"
                )
                hoff_ev = tx.append_event(
                    _make_cleanup_event(
                        event_type="resources.process_handoff_recorded",
                        auth_context=auth_context,
                        timestamp=request.timestamp,
                        correlation_id=f"phnd-{request.handoff_id}",
                        task_id=request.task_id,
                        run_id=request.run_id,
                        payload={
                            "department": "resources",
                            "handoff_id": request.handoff_id,
                            "cleanup_id": request.cleanup_id,
                            "retrospective_id": retro_id,
                            "handoff_status": h_status,
                            "deleted_temp_count": deleted_count,
                            "actual_cost_usd_total": (
                                res_summary.actual_cost_usd_total if res_summary is not None else None
                            ),
                            "estimated_cost_usd_total": (
                                res_summary.estimated_cost_usd_total if res_summary is not None else None
                            ),
                        },
                    )
                )
                saved_handoff = tx.insert_process_handoff(
                    ProcessLearningHandoffRecord(
                        handoff_id=request.handoff_id,
                        cleanup_id=request.cleanup_id,
                        task_id=request.task_id,
                        run_id=request.run_id,
                        retrospective_id=retro_id,
                        resource_summary=res_summary,
                        cleanup_inventory=inventory,
                        alerts_summary=alerts_snap,
                        handoff_status=h_status,
                        audit_event_id=hoff_ev.event_id,
                        created_by=auth_context.actor_id,
                        created_at=request.timestamp,
                    )
                )

            return PostTaskCleanupOutcome(
                cleanup=saved_cleanup,
                items=saved_items,
                handoff=saved_handoff,
                idempotent_replay=False,
            )

        return self._repo.execute_transaction(_tx_op)

    def get_handoff_for_task(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> ProcessLearningHandoffRecord | None:
        """Read durable Process learning handoff produced after cleanup."""
        if auth_context.actor_kind == "worker_agent" and task_id not in auth_context.assigned_task_ids:
            raise CleanupGovernanceError(
                "TASK_OWNERSHIP_DENIED",
                f"Worker '{auth_context.actor_id}' is not assigned to Task '{task_id}'.",
            )
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.read",
            resource=f"resources:cleanup:handoff:{task_id}",
            task_id=task_id,
            run_id=run_id,
            timestamp=timestamp,
            correlation_id=f"phnd-read-{task_id}",
        )
        return self._repo.get_handoff_for_task(task_id, run_id=run_id)

    def list_cleanups_for_task(
        self,
        task_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> list[PostTaskCleanupRecord]:
        """List cleanup records recorded for one Task."""
        if auth_context.actor_kind == "worker_agent" and task_id not in auth_context.assigned_task_ids:
            raise CleanupGovernanceError(
                "TASK_OWNERSHIP_DENIED",
                f"Worker '{auth_context.actor_id}' is not assigned to Task '{task_id}'.",
            )
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.read",
            resource=f"resources:cleanup:list:{task_id}",
            task_id=task_id,
            timestamp=timestamp,
            correlation_id=f"clnp-list-{task_id}",
        )
        return self._repo.list_cleanups_for_task(task_id)
