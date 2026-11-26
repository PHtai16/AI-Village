"""SQLite-backed durable Master Plan repository with immutable revision history."""

from __future__ import annotations

from datetime import datetime
import sqlite3

from core.contracts.event import Event
from core.contracts.references import ChecklistItem
from core.contracts.task import Task
from core.orchestration.planning_models import (
    MasterWorkPlan,
    PlanNotFoundError,
    PlanRevisionRecord,
    PlanValidationError,
    PlanningError,
    StalePlanVersionError,
    StoredMasterPlan,
)
from storage.repositories.errors import EntityNotFoundError, RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore


def _row_to_stored_plan(row: sqlite3.Row) -> StoredMasterPlan:
    """Rehydrate and validate ``StoredMasterPlan`` from a ``master_plans`` SQLite row."""
    plan = MasterWorkPlan.model_validate_json(str(row["plan_json"]))
    return StoredMasterPlan.model_validate(
        {
            "plan_id": row["plan_id"],
            "task_id": row["task_id"],
            "active_revision": int(row["active_revision"]),
            "record_version": int(row["record_version"]),
            "status": row["status"],
            "plan": plan,
            "plan_sha256": row["plan_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
            "last_audit_event_id": row["last_audit_event_id"],
        }
    )


def _row_to_plan_revision(row: sqlite3.Row) -> PlanRevisionRecord:
    """Rehydrate and validate ``PlanRevisionRecord`` from ``master_plan_revisions``."""
    plan = MasterWorkPlan.model_validate_json(str(row["plan_json"]))
    return PlanRevisionRecord.model_validate(
        {
            "plan_id": row["plan_id"],
            "task_id": row["task_id"],
            "plan_revision": int(row["plan_revision"]),
            "status": row["status"],
            "plan": plan,
            "plan_sha256": row["plan_sha256"],
            "change_summary": row["change_summary"],
            "changed_by": row["changed_by"],
            "changed_at": row["changed_at"],
            "audit_event_id": row["audit_event_id"],
        }
    )


class SQLitePlanRepository:
    """SQLite implementation of ``core.orchestration.planning_models.PlanRepository``."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        """Bind Master Plan repository to an open ``SQLiteRepositoryStore``."""
        self._store = store

    def _append_audit_or_raise(self, conn: sqlite3.Connection, audit_event: Event) -> None:
        """Append audit Event inside current transaction or raise fail-closed."""
        res = self._store.events.append_on_connection(conn, audit_event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Plan audit event append rejected."
            raise PlanningError("AUDIT_APPEND_FAILED", msg)

    def create_plan(
        self,
        record: StoredMasterPlan,
        initial_revision: PlanRevisionRecord,
        audit_event: Event,
    ) -> StoredMasterPlan:
        """Atomically persist Event, ``master_plans`` record, and immutable revision v1."""
        validated = StoredMasterPlan.model_validate_json(record.model_dump_json())
        rev_row = PlanRevisionRecord.model_validate_json(initial_revision.model_dump_json())

        with self._store.transaction_scope() as conn:
            self._append_audit_or_raise(conn, audit_event)
            try:
                conn.execute(
                    """
                    INSERT INTO master_plans (
                        plan_id, task_id, active_revision, record_version, status,
                        plan_json, plan_sha256, created_by, created_at,
                        updated_by, updated_at, last_audit_event_id
                    ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        validated.plan_id,
                        validated.task_id,
                        validated.active_revision,
                        validated.status,
                        validated.plan.canonical_json(),
                        validated.plan_sha256,
                        validated.created_by,
                        validated.created_at.isoformat(),
                        validated.updated_by,
                        validated.updated_at.isoformat(),
                        audit_event.event_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO master_plan_revisions (
                        plan_id, task_id, plan_revision, status, plan_json,
                        plan_sha256, change_summary, changed_by, changed_at, audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        rev_row.plan_id,
                        rev_row.task_id,
                        rev_row.plan_revision,
                        rev_row.status,
                        rev_row.plan.canonical_json(),
                        rev_row.plan_sha256,
                        rev_row.change_summary,
                        rev_row.changed_by,
                        rev_row.changed_at.isoformat(),
                        audit_event.event_id,
                    ),
                )
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to persist plan '{validated.plan_id}': {exc}",
                    entity_id=validated.plan_id,
                ) from exc

            row = conn.execute(
                "SELECT * FROM master_plans WHERE plan_id = ?;",
                (validated.plan_id,),
            ).fetchone()
            assert row is not None
            return _row_to_stored_plan(row)

    def revise_plan(
        self,
        record: StoredMasterPlan,
        new_revision: PlanRevisionRecord,
        *,
        expected_record_version: int,
        audit_event: Event,
    ) -> StoredMasterPlan:
        """Atomically update active plan under optimistic lock and append immutable revision."""
        validated = StoredMasterPlan.model_validate_json(record.model_dump_json())
        rev_row = PlanRevisionRecord.model_validate_json(new_revision.model_dump_json())

        with self._store.transaction_scope() as conn:
            current = conn.execute(
                "SELECT record_version FROM master_plans WHERE plan_id = ?;",
                (validated.plan_id,),
            ).fetchone()
            if current is None:
                raise PlanNotFoundError(
                    "PLAN_NOT_FOUND",
                    f"Plan '{validated.plan_id}' does not exist.",
                )
            if int(current["record_version"]) != expected_record_version:
                raise StalePlanVersionError(
                    "STALE_PLAN_VERSION",
                    (
                        f"Stale revision for plan '{validated.plan_id}': expected "
                        f"record_version={expected_record_version}, found {int(current['record_version'])}."
                    ),
                )

            self._append_audit_or_raise(conn, audit_event)
            try:
                cursor = conn.execute(
                    """
                    UPDATE master_plans
                    SET active_revision = ?,
                        record_version = record_version + 1,
                        status = ?,
                        plan_json = ?,
                        plan_sha256 = ?,
                        updated_by = ?,
                        updated_at = ?,
                        last_audit_event_id = ?
                    WHERE plan_id = ? AND record_version = ?;
                    """,
                    (
                        validated.active_revision,
                        validated.status,
                        validated.plan.canonical_json(),
                        validated.plan_sha256,
                        validated.updated_by,
                        validated.updated_at.isoformat(),
                        audit_event.event_id,
                        validated.plan_id,
                        expected_record_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StalePlanVersionError(
                        "STALE_PLAN_VERSION",
                        f"Concurrent modification detected on plan '{validated.plan_id}'.",
                    )
                conn.execute(
                    """
                    INSERT INTO master_plan_revisions (
                        plan_id, task_id, plan_revision, status, plan_json,
                        plan_sha256, change_summary, changed_by, changed_at, audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        rev_row.plan_id,
                        rev_row.task_id,
                        rev_row.plan_revision,
                        rev_row.status,
                        rev_row.plan.canonical_json(),
                        rev_row.plan_sha256,
                        rev_row.change_summary,
                        rev_row.changed_by,
                        rev_row.changed_at.isoformat(),
                        audit_event.event_id,
                    ),
                )
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to revise plan '{validated.plan_id}': {exc}",
                    entity_id=validated.plan_id,
                ) from exc

            row = conn.execute(
                "SELECT * FROM master_plans WHERE plan_id = ?;",
                (validated.plan_id,),
            ).fetchone()
            assert row is not None
            return _row_to_stored_plan(row)

    def get_plan(self, plan_id: str) -> StoredMasterPlan | None:
        """Fetch active Master plan snapshot by ``plan_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM master_plans WHERE plan_id = ?;",
                (plan_id,),
            ).fetchone()
            return _row_to_stored_plan(row) if row is not None else None

    def get_active_plan_for_task(self, task_id: str) -> StoredMasterPlan | None:
        """Fetch latest persisted Master plan linked to ``task_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM master_plans
                WHERE task_id = ?
                ORDER BY updated_at DESC, record_version DESC, plan_id ASC
                LIMIT 1;
                """,
                (task_id,),
            ).fetchone()
            return _row_to_stored_plan(row) if row is not None else None

    def list_plan_revisions(self, plan_id: str) -> list[PlanRevisionRecord]:
        """List immutable revision history for ``plan_id`` ordered chronologically."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM master_plan_revisions
                WHERE plan_id = ?
                ORDER BY plan_revision ASC;
                """,
                (plan_id,),
            ).fetchall()
            return [_row_to_plan_revision(row) for row in rows]

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
        """Atomically materialize PENDING ChecklistItems, bind to Task, and emit Event."""
        validated_plan = StoredMasterPlan.model_validate_json(updated_plan.model_dump_json())
        validated_items = [
            ChecklistItem.model_validate_json(item.model_dump_json())
            for item in checklist_items
        ]
        for item in validated_items:
            if item.task_id != validated_plan.task_id:
                raise PlanValidationError(
                    "CHECKLIST_TASK_MISMATCH",
                    f"ChecklistItem '{item.checklist_item_id}' does not match task '{validated_plan.task_id}'.",
                )
            if item.status != "PENDING" or item.completed_at is not None or item.evidence_reference is not None:
                raise PlanValidationError(
                    "INVALID_INITIAL_CHECKLIST_STATE",
                    "Planned ChecklistItems must start strictly PENDING with null completion/evidence.",
                )

        with self._store.transaction_scope() as conn:
            plan_row = conn.execute(
                "SELECT record_version FROM master_plans WHERE plan_id = ?;",
                (plan_id,),
            ).fetchone()
            if plan_row is None:
                raise PlanNotFoundError("PLAN_NOT_FOUND", f"Plan '{plan_id}' does not exist.")
            if int(plan_row["record_version"]) != expected_plan_record_version:
                raise StalePlanVersionError(
                    "STALE_PLAN_VERSION",
                    (
                        f"Stale apply for plan '{plan_id}': expected record_version="
                        f"{expected_plan_record_version}, found {int(plan_row['record_version'])}."
                    ),
                )

            stored_task = self._store.tasks.get_stored_task_on_connection(conn, validated_plan.task_id)
            if stored_task is None:
                raise EntityNotFoundError(
                    f"Task '{validated_plan.task_id}' does not exist.",
                    entity_id=validated_plan.task_id,
                )
            if stored_task.task.status in {"RUNNING", "COMPLETED", "FAILED", "CANCELLED"}:
                raise PlanValidationError(
                    "TASK_STATE_FORBIDS_PLAN_APPLY",
                    f"Cannot apply plan while Task '{stored_task.task.task_id}' is '{stored_task.task.status}'.",
                )

            self._append_audit_or_raise(conn, audit_event)

            try:
                cursor = conn.execute(
                    """
                    UPDATE master_plans
                    SET status = ?,
                        record_version = record_version + 1,
                        plan_json = ?,
                        plan_sha256 = ?,
                        updated_by = ?,
                        updated_at = ?,
                        last_audit_event_id = ?
                    WHERE plan_id = ? AND record_version = ?;
                    """,
                    (
                        validated_plan.status,
                        validated_plan.plan.canonical_json(),
                        validated_plan.plan_sha256,
                        validated_plan.updated_by,
                        validated_plan.updated_at.isoformat(),
                        audit_event.event_id,
                        plan_id,
                        expected_plan_record_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StalePlanVersionError(
                        "STALE_PLAN_VERSION",
                        f"Concurrent modification detected applying plan '{plan_id}'.",
                    )
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed updating applied plan '{plan_id}': {exc}",
                    entity_id=plan_id,
                ) from exc

            persisted_items = [
                self._store.metadata.save_checklist_item(item) for item in validated_items
            ]

            merged_criteria = list(stored_task.task.acceptance_criteria)
            for crit in validated_plan.plan.acceptance_criteria:
                if crit not in merged_criteria:
                    merged_criteria.append(crit)

            new_checklist_ids = [item.checklist_item_id for item in persisted_items]
            next_updated_at = updated_at if stored_task.task.created_at is not None else None
            task_data = stored_task.task.model_dump()
            task_data.update(
                {
                    "checklist_item_ids": new_checklist_ids,
                    "acceptance_criteria": merged_criteria,
                    "updated_at": next_updated_at,
                }
            )
            updated_task = Task.model_validate(task_data)
            self._store.tasks.save_task_state_on_connection(
                conn,
                updated_task,
                expected_version=stored_task.version,
            )

            refreshed_plan_row = conn.execute(
                "SELECT * FROM master_plans WHERE plan_id = ?;",
                (plan_id,),
            ).fetchone()
            assert refreshed_plan_row is not None
            return _row_to_stored_plan(refreshed_plan_row), persisted_items
