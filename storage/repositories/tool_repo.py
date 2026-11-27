"""SQLite persistence implementation for Phase 07 Tool Registry and immutable Tool Execution Records."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from typing import Any

from core.tools.models import (
    ToolCategory,
    ToolExecutionError,
    ToolExecutionRecord,
    ToolManifest,
    ToolResultStatus,
    ToolStatus,
    ToolUsageSummary,
)
from core.tools.runtime import aggregate_tool_usage
from storage.repositories.errors import DuplicateEntityError, EntityNotFoundError, RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore


def _dumps(value: Any) -> str:
    """Serialize deterministic compact JSON for SQLite TEXT columns."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row_to_manifest(row: sqlite3.Row) -> ToolManifest:
    """Rehydrate a validated ToolManifest from SQLite `tool_registry`."""
    return ToolManifest.model_validate_json(str(row["manifest_json"]))


def _row_to_execution(row: sqlite3.Row) -> ToolExecutionRecord:
    """Rehydrate a validated secret-redacted ToolExecutionRecord from SQLite."""
    return ToolExecutionRecord.model_validate(
        {
            "execution_id": row["execution_id"],
            "tool_id": row["tool_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "session_id": row["session_id"],
            "actor_id": row["actor_id"],
            "action": row["action"],
            "status": row["status"],
            "business_verification_status": row["business_verification_status"],
            "duration_ms": int(row["duration_ms"]),
            "estimated_cost_usd": row["estimated_cost_usd"],
            "approval_id": row["approval_id"],
            "audit_event_id": row["audit_event_id"],
            "redacted_input": json.loads(str(row["redacted_input_json"])),
            "redacted_output": json.loads(str(row["redacted_output_json"])),
            "artifact_ids": json.loads(str(row["artifact_ids_json"])),
            "evidence_refs": json.loads(str(row["evidence_refs_json"])),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "executed_at": row["executed_at"],
        }
    )


class SqliteToolRepository:
    """SQLite implementation of `core.tools.models.ToolRepository`."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        """Bind Tool repository to an initialized SQLiteRepositoryStore."""
        self._store = store

    def upsert_tool(
        self,
        manifest: ToolManifest,
        *,
        registered_at: datetime,
        updated_at: datetime,
    ) -> ToolManifest:
        """Persist or update a registered ToolManifest inside a transaction."""
        validated = ToolManifest.model_validate_json(manifest.model_dump_json())
        manifest_json = _dumps(validated.model_dump(mode="json"))
        reg_iso = registered_at.isoformat()
        upd_iso = updated_at.isoformat()

        with self._store.transaction_scope() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO tool_registry (
                        tool_id, version, category, status, execution_mode,
                        risk_level, manifest_json, registered_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tool_id) DO UPDATE SET
                        version = excluded.version,
                        category = excluded.category,
                        status = excluded.status,
                        execution_mode = excluded.execution_mode,
                        risk_level = excluded.risk_level,
                        manifest_json = excluded.manifest_json,
                        updated_at = excluded.updated_at;
                    """,
                    (
                        validated.id,
                        validated.version,
                        validated.category,
                        validated.status,
                        validated.execution_mode,
                        validated.risk_level,
                        manifest_json,
                        reg_iso,
                        upd_iso,
                    ),
                )
            except sqlite3.Error as exc:
                raise RepositoryError("PERSISTENCE_FAILED", f"Failed to persist tool '{validated.id}': {exc}", entity_id=validated.id) from exc

            row = conn.execute(
                "SELECT * FROM tool_registry WHERE tool_id = ?;",
                (validated.id,),
            ).fetchone()
            assert row is not None
            return _row_to_manifest(row)

    def get_tool(self, tool_id: str) -> ToolManifest | None:
        """Load a ToolManifest from `tool_registry` by canonical Tool ID."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM tool_registry WHERE tool_id = ?;",
                (tool_id,),
            ).fetchone()
            return _row_to_manifest(row) if row is not None else None

    def list_tools(
        self,
        *,
        category: ToolCategory | None = None,
        status: ToolStatus | None = None,
    ) -> list[ToolManifest]:
        """List persisted ToolManifests ordered by tool_id."""
        clauses: list[str] = []
        params: list[object] = []
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = f"SELECT * FROM tool_registry {where_sql}ORDER BY tool_id ASC;"
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_manifest(row) for row in rows]

    def set_tool_status(
        self,
        tool_id: str,
        status: ToolStatus,
        *,
        updated_at: datetime,
    ) -> ToolManifest:
        """Update persisted availability status on an existing registered tool."""
        with self._store.transaction_scope() as conn:
            row = conn.execute(
                "SELECT * FROM tool_registry WHERE tool_id = ?;",
                (tool_id,),
            ).fetchone()
            if row is None:
                raise ToolExecutionError("TOOL_NOT_FOUND", f"Tool '{tool_id}' is not registered.")
            existing = _row_to_manifest(row)
            updated = existing.model_copy(update={"status": status})
            manifest_json = _dumps(updated.model_dump(mode="json"))
            conn.execute(
                """
                UPDATE tool_registry
                SET status = ?, manifest_json = ?, updated_at = ?
                WHERE tool_id = ?;
                """,
                (status, manifest_json, updated_at.isoformat(), tool_id),
            )
            return updated

    def record_execution(self, record: ToolExecutionRecord) -> ToolExecutionRecord:
        """Persist an immutable secret-redacted ToolExecutionRecord."""
        validated = ToolExecutionRecord.model_validate_json(record.model_dump_json())
        with self._store.transaction_scope() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO tool_executions (
                        execution_id, tool_id, task_id, run_id, session_id, actor_id,
                        action, status, business_verification_status, duration_ms,
                        estimated_cost_usd, approval_id, audit_event_id,
                        redacted_input_json, redacted_output_json,
                        artifact_ids_json, evidence_refs_json,
                        error_code, error_message, executed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        validated.execution_id,
                        validated.tool_id,
                        validated.task_id,
                        validated.run_id,
                        validated.session_id,
                        validated.actor_id,
                        validated.action,
                        validated.status,
                        validated.business_verification_status,
                        validated.duration_ms,
                        validated.estimated_cost_usd,
                        validated.approval_id,
                        validated.audit_event_id,
                        _dumps(validated.redacted_input),
                        _dumps(validated.redacted_output),
                        _dumps(validated.artifact_ids),
                        _dumps(validated.evidence_refs),
                        validated.error_code,
                        validated.error_message,
                        validated.executed_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                detail = str(exc)
                if "primary key" in detail.lower() or "unique" in detail.lower():
                    raise DuplicateEntityError(
                        f"Duplicate execution_id '{validated.execution_id}': {detail}",
                        entity_id=validated.execution_id,
                    ) from exc
                raise RepositoryError("PERSISTENCE_FAILED", detail, entity_id=validated.execution_id) from exc
            except sqlite3.Error as exc:
                raise RepositoryError("PERSISTENCE_FAILED", str(exc), entity_id=validated.execution_id) from exc
            return validated

    def list_executions(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        tool_id: str | None = None,
        status: ToolResultStatus | None = None,
        limit: int = 200,
    ) -> list[ToolExecutionRecord]:
        """List persisted execution records matching optional filters ordered chronologically."""
        clauses: list[str] = []
        params: list[object] = []
        for col, val in (
            ("task_id", task_id),
            ("run_id", run_id),
            ("session_id", session_id),
            ("actor_id", actor_id),
            ("tool_id", tool_id),
            ("status", status),
        ):
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        limit_sql = "LIMIT ?" if limit > 0 else ""
        if limit > 0:
            params.append(limit)
        query = (
            f"SELECT * FROM tool_executions {where_sql}"
            f"ORDER BY executed_at ASC, execution_id ASC {limit_sql};"
        )
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_execution(row) for row in rows]

    def summarize_usage(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        actor_id: str | None = None,
    ) -> ToolUsageSummary:
        """Aggregate usage metrics across all matching persisted executions."""
        records = self.list_executions(
            task_id=task_id,
            run_id=run_id,
            actor_id=actor_id,
            limit=0,
        )
        return aggregate_tool_usage(
            records,
            task_id=task_id,
            run_id=run_id,
            actor_id=actor_id,
        )


__all__ = ["SqliteToolRepository"]
