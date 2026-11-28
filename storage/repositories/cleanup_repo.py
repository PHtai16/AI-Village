"""SQLite persistence & managed-temp safety executor for P11.05 Safe Post-Task Cleanup & Process Handoff."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Literal

from core.contracts.event import Event
from core.contracts.run import Run
from core.contracts.task import Task
from core.departments.resources_models import TaskResourceUsageSummary
from core.optimization.cleanup_models import (
    CleanupInventorySummary,
    CleanupItemRecord,
    CleanupPersistenceError,
    CleanupTransactionContext,
    PostTaskCleanupOutcome,
    PostTaskCleanupRecord,
    ProcessLearningHandoffRecord,
    ProtectedRefsSnapshot,
    TempDeleteExecutionResult,
)
from storage.repositories.errors import RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore, _row_to_run, _row_to_task


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _norm_path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _expand_path_keys(paths: set[str], root: Path) -> set[str]:
    keys: set[str] = set()
    for s in paths:
        if not s:
            continue
        p = Path(s)
        keys.add(_norm_path_key(p))
        if not p.is_absolute():
            keys.add(_norm_path_key(root / p))
    return keys


def _contains_symlink(candidate: Path, root: Path) -> bool:
    """Detect symlink at target or any ancestor within ``root`` without resolving through symlinks."""
    probe = candidate
    root_norm = os.path.normcase(str(root))
    while True:
        if os.path.islink(probe):
            return True
        if os.path.normcase(str(probe)) == root_norm:
            break
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    if candidate.exists() and not os.path.islink(candidate) and candidate.is_dir():
        for dirpath, dirnames, filenames in os.walk(candidate, followlinks=False):
            dp = Path(dirpath)
            for name in (*dirnames, *filenames):
                if os.path.islink(dp / name):
                    return True
    return False


def _measure_size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        total = 0
        for dirpath, _, filenames in os.walk(path, followlinks=False):
            dp = Path(dirpath)
            for fname in filenames:
                fp = dp / fname
                if not os.path.islink(fp) and fp.is_file():
                    total += fp.stat().st_size
        return total
    return 0


class ManagedTempCleanupExecutor:
    """Strictly contained temp cleaner allowing deletes ONLY inside ``managed_temp_root``."""

    def __init__(self, managed_temp_root: Path) -> None:
        """Bind executor to an explicit temporary scratch directory root."""
        self._root = managed_temp_root.resolve(strict=False)
        self._root_norm = os.path.normcase(str(self._root))

    @property
    def managed_temp_root(self) -> Path:
        """Return resolved containment directory root."""
        return self._root

    def delete_managed_temp_target(
        self,
        raw_path: str,
        *,
        resource_kind: Literal["TEMP_FILE", "TEMP_DIR"],
        protected_paths: set[str],
        shared_paths: set[str],
    ) -> TempDeleteExecutionResult:
        """Validate path containment, symlinks, shared refs & protected paths before deleting."""
        if not raw_path or "\x00" in raw_path:
            return TempDeleteExecutionResult(
                target_ref=raw_path,
                resource_kind=resource_kind,
                action="BLOCKED_UNSAFE_PATH",
                reason="Invalid empty or NUL-containing path.",
            )

        p = Path(raw_path)
        lexical = p if p.is_absolute() else (self._root / p)

        # Reject any symlink component or child symlink before resolving targets.
        if _contains_symlink(lexical, self._root):
            return TempDeleteExecutionResult(
                target_ref=raw_path,
                resource_kind=resource_kind,
                action="BLOCKED_UNSAFE_PATH",
                reason="Blocked symlink inside or crossing managed_temp_root.",
            )

        resolved = lexical.resolve(strict=False)
        try:
            is_inside = resolved.is_relative_to(self._root)
        except ValueError:
            is_inside = False
        if not is_inside or resolved == self._root:
            return TempDeleteExecutionResult(
                target_ref=raw_path,
                resource_kind=resource_kind,
                action="BLOCKED_UNSAFE_PATH",
                reason="Target escapes or equals managed_temp_root; deletion forbidden.",
            )

        norm_target = _norm_path_key(resolved)
        norm_shared = _expand_path_keys(shared_paths, self._root)
        norm_protected = _expand_path_keys(protected_paths, self._root)
        norm_raw = os.path.normcase(str(p))

        if norm_target in norm_shared or norm_raw in norm_shared or str(p) in shared_paths:
            return TempDeleteExecutionResult(
                target_ref=raw_path,
                resource_kind=resource_kind,
                action="BLOCKED_SHARED_REF",
                reason="Preserved path explicitly shared with or protected for another Task.",
            )

        if norm_target in norm_protected or norm_raw in norm_protected or str(p) in protected_paths:
            return TempDeleteExecutionResult(
                target_ref=raw_path,
                resource_kind=resource_kind,
                action="BLOCKED_UNSAFE_PATH",
                reason="Target matches protected Vault artifact, evidence, or database file.",
            )

        if not resolved.exists():
            return TempDeleteExecutionResult(
                target_ref=raw_path,
                resource_kind=resource_kind,
                action="PRESERVED",
                reason="Temporary target already absent on disk.",
                size_bytes=0,
            )

        size_bytes = _measure_size_bytes(resolved)
        try:
            if resolved.is_dir():
                if resource_kind != "TEMP_DIR":
                    return TempDeleteExecutionResult(
                        target_ref=raw_path,
                        resource_kind=resource_kind,
                        action="BLOCKED_UNSAFE_PATH",
                        reason="Directory target requires explicit resource_kind='TEMP_DIR'.",
                    )
                shutil.rmtree(resolved)
            else:
                resolved.unlink()
        except OSError as exc:
            return TempDeleteExecutionResult(
                target_ref=raw_path,
                resource_kind=resource_kind,
                action="FAILED",
                reason=f"Filesystem cleanup error: {exc}",
            )

        return TempDeleteExecutionResult(
            target_ref=raw_path,
            resource_kind=resource_kind,
            action="DELETED",
            reason="Deleted safely inside managed_temp_root.",
            size_bytes=size_bytes,
        )


def _row_to_cleanup(row: sqlite3.Row) -> PostTaskCleanupRecord:
    return PostTaskCleanupRecord.model_validate(
        {
            "cleanup_id": row["cleanup_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "task_status_snapshot": row["task_status_snapshot"],
            "cleanup_status": row["cleanup_status"],
            "idempotency_key": row["idempotency_key"],
            "request_fingerprint": row["request_fingerprint"],
            "inventory": CleanupInventorySummary.model_validate_json(str(row["inventory_json"])),
            "preserved_refs": json.loads(str(row["preserved_refs_json"])),
            "handoff_id": row["handoff_id"],
            "audit_event_id": row["audit_event_id"],
            "performed_by": row["performed_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "record_version": int(row["record_version"]),
        }
    )


def _row_to_item(row: sqlite3.Row) -> CleanupItemRecord:
    return CleanupItemRecord.model_validate(
        {
            "item_id": row["item_id"],
            "cleanup_id": row["cleanup_id"],
            "resource_kind": row["resource_kind"],
            "target_ref": row["target_ref"],
            "action": row["action"],
            "reason": row["reason"],
            "size_bytes": int(row["size_bytes"]) if row["size_bytes"] is not None else None,
            "created_at": row["created_at"],
        }
    )


def _row_to_handoff(row: sqlite3.Row) -> ProcessLearningHandoffRecord:
    raw_res = json.loads(str(row["resource_summary_json"]))
    return ProcessLearningHandoffRecord.model_validate(
        {
            "handoff_id": row["handoff_id"],
            "cleanup_id": row["cleanup_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "retrospective_id": row["retrospective_id"],
            "resource_summary": (
                TaskResourceUsageSummary.model_validate(raw_res) if raw_res is not None else None
            ),
            "cleanup_inventory": CleanupInventorySummary.model_validate_json(
                str(row["cleanup_inventory_json"])
            ),
            "alerts_summary": json.loads(str(row["alerts_summary_json"])),
            "handoff_status": row["handoff_status"],
            "audit_event_id": row["audit_event_id"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
    )


class _SQLiteCleanupTransactionContext:
    """Transaction-scoped SQLite implementation of ``CleanupTransactionContext``."""

    def __init__(self, store: SQLiteRepositoryStore, conn: sqlite3.Connection) -> None:
        self._store = store
        self._conn = conn

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?;", (task_id,)).fetchone()
        return _row_to_task(row) if row is not None else None

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?;", (run_id,)).fetchone()
        return _row_to_run(row) if row is not None else None

    def find_cleanup_by_idempotency(
        self, task_id: str, run_id: str | None, idempotency_key: str
    ) -> PostTaskCleanupRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM post_task_cleanups
            WHERE task_id = ? AND COALESCE(run_id, '') = COALESCE(?, '') AND idempotency_key = ?;
            """,
            (task_id, run_id, idempotency_key),
        ).fetchone()
        return _row_to_cleanup(row) if row is not None else None

    def list_cleanup_items(self, cleanup_id: str) -> list[CleanupItemRecord]:
        rows = self._conn.execute(
            "SELECT * FROM post_task_cleanup_items WHERE cleanup_id = ? ORDER BY rowid ASC;",
            (cleanup_id,),
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    def get_handoff_by_id(self, handoff_id: str) -> ProcessLearningHandoffRecord | None:
        row = self._conn.execute(
            "SELECT * FROM process_learning_handoffs WHERE handoff_id = ?;",
            (handoff_id,),
        ).fetchone()
        return _row_to_handoff(row) if row is not None else None

    def get_handoff_for_task_run(
        self, task_id: str, run_id: str | None
    ) -> ProcessLearningHandoffRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM process_learning_handoffs
            WHERE task_id = ? AND COALESCE(run_id, '') = COALESCE(?, '')
            LIMIT 1;
            """,
            (task_id, run_id),
        ).fetchone()
        return _row_to_handoff(row) if row is not None else None

    def collect_protected_refs(self, task_id: str) -> ProtectedRefsSnapshot:
        protected_paths: list[str] = [str(self._store.config.db_path)]
        shared_paths: list[str] = []

        # Protect ALL Vault artifacts & blobs belonging to any Task, and flag cross-task shared refs.
        art_rows = self._conn.execute(
            """
            SELECT a.artifact_id, a.task_id, a.relative_path, b.storage_relpath
            FROM artifact_registry_entries a
            JOIN vault_blobs b ON b.sha256 = a.blob_sha256
            WHERE a.lifecycle_status != 'PURGED'
            ORDER BY a.artifact_id ASC;
            """
        ).fetchall()
        task_artifact_ids: list[str] = []
        for r in art_rows:
            rel_p = str(r["relative_path"])
            blob_p = str(r["storage_relpath"])
            protected_paths.extend([rel_p, blob_p])
            if str(r["task_id"]) == task_id:
                task_artifact_ids.append(str(r["artifact_id"]))
            else:
                shared_paths.extend([rel_p, blob_p])

        shared_rows = self._conn.execute(
            """
            SELECT DISTINCT r.artifact_id, a.relative_path, b.storage_relpath
            FROM artifact_task_references r
            JOIN artifact_registry_entries a ON a.artifact_id = r.artifact_id
            JOIN vault_blobs b ON b.sha256 = a.blob_sha256
            WHERE a.task_id = ? OR r.referencing_task_id = ?;
            """,
            (task_id, task_id),
        ).fetchall()
        shared_art_ids = [str(r["artifact_id"]) for r in shared_rows]
        for r in shared_rows:
            shared_paths.extend([str(r["relative_path"]), str(r["storage_relpath"])])

        ev_rows = self._conn.execute(
            "SELECT evidence_id FROM evidence_records WHERE task_id = ? ORDER BY created_at ASC;",
            (task_id,),
        ).fetchall()
        evidence_ids = [str(r["evidence_id"]) for r in ev_rows]

        led_rows = self._conn.execute(
            "SELECT ledger_entry_id FROM resource_ledger_entries WHERE task_id = ? ORDER BY recorded_at ASC;",
            (task_id,),
        ).fetchall()
        ledger_ids = [str(r["ledger_entry_id"]) for r in led_rows]

        return ProtectedRefsSnapshot(
            protected_paths=list(dict.fromkeys(protected_paths)),
            shared_paths=list(dict.fromkeys(shared_paths)),
            artifact_ids=task_artifact_ids,
            shared_artifact_ids=list(dict.fromkeys(shared_art_ids)),
            evidence_ids=evidence_ids,
            ledger_entry_ids=ledger_ids,
        )

    def release_lingering_leases(
        self, task_id: str, *, run_id: str | None, released_at: datetime, reason: str
    ) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT lease_id, queue_entry_id FROM scheduler_execution_leases
            WHERE task_id = ?
              AND (? IS NULL OR run_id = ?)
              AND state IN ('RESERVED', 'ACTIVE')
            ORDER BY acquired_at ASC;
            """,
            (task_id, run_id, run_id),
        ).fetchall()
        released_ids: list[str] = []
        for r in rows:
            lid = str(r["lease_id"])
            qid = str(r["queue_entry_id"])
            self._conn.execute(
                """
                UPDATE scheduler_execution_leases
                SET state = 'RELEASED',
                    released_at = ?,
                    release_reason = ?,
                    record_version = record_version + 1
                WHERE lease_id = ?;
                """,
                (released_at.isoformat(), reason, lid),
            )
            self._conn.execute(
                """
                UPDATE scheduler_queue_entries
                SET status = CASE WHEN status IN ('RESERVED', 'RUNNING', 'QUEUED', 'READY')
                                  THEN 'COMPLETED' ELSE status END,
                    completed_at = COALESCE(completed_at, ?),
                    updated_at = ?,
                    record_version = record_version + 1
                WHERE queue_entry_id = ?;
                """,
                (released_at.isoformat(), released_at.isoformat(), qid),
            )
            released_ids.append(lid)
        return released_ids

    def summarize_task_resources(self, task_id: str) -> TaskResourceUsageSummary | None:
        return self._store.resources.summarize_task(task_id)

    def get_alerts_snapshot(self, task_id: str) -> dict[str, Any]:
        rows = self._conn.execute(
            """
            SELECT alert_id, alert_category, severity, status, observed_value, threshold_limit, is_estimated_basis
            FROM resource_threshold_alerts
            WHERE task_id = ? OR (scope_kind = 'TASK' AND scope_ref = ?)
            ORDER BY created_at ASC;
            """,
            (task_id, task_id),
        ).fetchall()
        return {
            "total_alerts_count": len(rows),
            "open_alerts_count": sum(1 for r in rows if str(r["status"]) in ("OPEN", "ACKNOWLEDGED")),
            "critical_alerts_count": sum(
                1 for r in rows if str(r["severity"]) == "CRITICAL" and str(r["status"]) in ("OPEN", "ACKNOWLEDGED")
            ),
            "alerts": [
                {
                    "alert_id": str(r["alert_id"]),
                    "category": str(r["alert_category"]),
                    "severity": str(r["severity"]),
                    "status": str(r["status"]),
                    "observed_value": float(r["observed_value"]),
                    "threshold_limit": float(r["threshold_limit"]),
                    "is_estimated_basis": bool(int(r["is_estimated_basis"])),
                }
                for r in rows
            ],
        }

    def get_retrospective_id_for_task(self, task_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT retrospective_id FROM task_retrospectives WHERE task_id = ?;",
            (task_id,),
        ).fetchone()
        return str(row["retrospective_id"]) if row is not None else None

    def insert_cleanup_record(self, record: PostTaskCleanupRecord) -> PostTaskCleanupRecord:
        validated = PostTaskCleanupRecord.model_validate_json(record.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO post_task_cleanups (
                cleanup_id, task_id, run_id, task_status_snapshot, cleanup_status,
                idempotency_key, request_fingerprint, inventory_json, preserved_refs_json,
                handoff_id, audit_event_id, performed_by, created_at, updated_at, record_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                validated.cleanup_id,
                validated.task_id,
                validated.run_id,
                validated.task_status_snapshot,
                validated.cleanup_status,
                validated.idempotency_key,
                validated.request_fingerprint,
                validated.inventory.model_dump_json(),
                _dumps(validated.preserved_refs),
                validated.handoff_id,
                validated.audit_event_id,
                validated.performed_by,
                validated.created_at.isoformat(),
                validated.updated_at.isoformat(),
                validated.record_version,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM post_task_cleanups WHERE cleanup_id = ?;",
            (validated.cleanup_id,),
        ).fetchone()
        assert row is not None
        return _row_to_cleanup(row)

    def insert_cleanup_items(self, items: list[CleanupItemRecord]) -> list[CleanupItemRecord]:
        saved: list[CleanupItemRecord] = []
        for item in items:
            v = CleanupItemRecord.model_validate_json(item.model_dump_json())
            self._conn.execute(
                """
                INSERT INTO post_task_cleanup_items (
                    item_id, cleanup_id, resource_kind, target_ref, action, reason, size_bytes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    v.item_id,
                    v.cleanup_id,
                    v.resource_kind,
                    v.target_ref,
                    v.action,
                    v.reason,
                    v.size_bytes,
                    v.created_at.isoformat(),
                ),
            )
            saved.append(v)
        return saved

    def insert_process_handoff(
        self, handoff: ProcessLearningHandoffRecord
    ) -> ProcessLearningHandoffRecord:
        validated = ProcessLearningHandoffRecord.model_validate_json(handoff.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO process_learning_handoffs (
                handoff_id, cleanup_id, task_id, run_id, retrospective_id,
                resource_summary_json, cleanup_inventory_json, alerts_summary_json,
                handoff_status, audit_event_id, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                validated.handoff_id,
                validated.cleanup_id,
                validated.task_id,
                validated.run_id,
                validated.retrospective_id,
                validated.resource_summary.model_dump_json()
                if validated.resource_summary is not None
                else "null",
                validated.cleanup_inventory.model_dump_json(),
                _dumps(validated.alerts_summary),
                validated.handoff_status,
                validated.audit_event_id,
                validated.created_by,
                validated.created_at.isoformat(),
            ),
        )
        loaded = self.get_handoff_by_id(validated.handoff_id)
        assert loaded is not None
        return loaded

    def append_event(self, event: Event) -> Event:
        res = self._store.events.append_on_connection(self._conn, event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Audit event append rejected."
            raise CleanupPersistenceError("AUDIT_EVENT_REJECTED", msg)
        return res.entry.event


class SQLiteCleanupRepository:
    """SQLite repository implementing durable P11.05 post-task cleanup and Process handoffs."""

    def __init__(
        self,
        store: SQLiteRepositoryStore,
        *,
        before_commit_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        self._store = store
        self._before_commit_hook = before_commit_hook

    def execute_transaction(
        self,
        processor: Callable[[CleanupTransactionContext], Any],
    ) -> Any:
        """Execute ``processor`` atomically inside one ``BEGIN IMMEDIATE`` transaction."""
        try:
            with self._store.transaction_scope() as conn:
                tx = _SQLiteCleanupTransactionContext(self._store, conn)
                result = processor(tx)
                if self._before_commit_hook is not None:
                    self._before_commit_hook(conn)
                return result
        except (sqlite3.Error, RepositoryError) as exc:
            raise CleanupPersistenceError(
                "CLEANUP_PERSISTENCE_FAILED",
                f"Atomic cleanup transaction failed and rolled back: {exc}",
            ) from exc

    def get_cleanup(self, cleanup_id: str) -> PostTaskCleanupOutcome | None:
        """Fetch persisted cleanup outcome by ``cleanup_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM post_task_cleanups WHERE cleanup_id = ?;",
                (cleanup_id,),
            ).fetchone()
            if row is None:
                return None
            rec = _row_to_cleanup(row)
            items = [
                _row_to_item(r)
                for r in conn.execute(
                    "SELECT * FROM post_task_cleanup_items WHERE cleanup_id = ? ORDER BY rowid ASC;",
                    (cleanup_id,),
                ).fetchall()
            ]
            hoff = (
                conn.execute(
                    "SELECT * FROM process_learning_handoffs WHERE handoff_id = ?;",
                    (rec.handoff_id,),
                ).fetchone()
                if rec.handoff_id is not None
                else None
            )
            return PostTaskCleanupOutcome(
                cleanup=rec,
                items=items,
                handoff=_row_to_handoff(hoff) if hoff is not None else None,
                idempotent_replay=False,
            )

    def list_cleanups_for_task(self, task_id: str) -> list[PostTaskCleanupRecord]:
        """List cleanup records for ``task_id`` ordered chronologically."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM post_task_cleanups WHERE task_id = ? ORDER BY created_at ASC, cleanup_id ASC;",
                (task_id,),
            ).fetchall()
            return [_row_to_cleanup(r) for r in rows]

    def get_handoff_for_task(
        self, task_id: str, *, run_id: str | None = None
    ) -> ProcessLearningHandoffRecord | None:
        """Fetch durable Process learning handoff for ``task_id`` and optional ``run_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM process_learning_handoffs
                WHERE task_id = ? AND (? IS NULL OR COALESCE(run_id, '') = ?)
                ORDER BY created_at DESC LIMIT 1;
                """,
                (task_id, run_id, run_id or ""),
            ).fetchone()
            return _row_to_handoff(row) if row is not None else None
