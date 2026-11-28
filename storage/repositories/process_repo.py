"""SQLite persistence and read-only execution bundle assembly for P10.01 Process Retrospectives."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from typing import Any

from core.contracts.event import Event
from core.contracts.identifiers import validate_aware_datetime, validate_safe_payload
from core.departments.process_models import (
    AgentSkillSnapshot,
    DuplicateRetrospectiveConflictError,
    ImprovementStatus,
    PatternCategory,
    ProcessRetrospectiveRecord,
    ProcessTaskNotFoundError,
    ProcessValidationError,
    ResolvedConflictFact,
    StaleRetrospectiveVersionError,
    StoredTaskRetrospective,
    SuggestedImprovement,
    TaskArtifactFact,
    TaskEvidenceFact,
    TaskExecutionEvidenceBundle,
    TaskExecutionSummary,
    TaskRetrospectiveRevision,
    WorkerStepSnapshot,
    scrub_text,
)
from storage.repositories.errors import DuplicateEntityError, RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore


def _dumps(value: Any) -> str:
    """Serialize deterministic compact JSON for SQLite storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row_to_stored_retrospective(
    row: sqlite3.Row,
    *,
    linked_run_ids: list[str],
    summary_override: TaskExecutionSummary | None = None,
    legacy_override: ProcessRetrospectiveRecord | None = None,
) -> StoredTaskRetrospective:
    """Rehydrate and validate ``StoredTaskRetrospective`` from SQLite row data."""
    summary = summary_override or TaskExecutionSummary.model_validate_json(str(row["summary_json"]))
    raw_legacy = row["legacy_record_json"]
    legacy = legacy_override or (
        ProcessRetrospectiveRecord.model_validate_json(str(raw_legacy))
        if raw_legacy is not None
        else None
    )
    return StoredTaskRetrospective.model_validate(
        {
            "retrospective_id": row["retrospective_id"],
            "task_id": row["task_id"],
            "primary_run_id": row["primary_run_id"],
            "execution_status": row["execution_status"],
            "active_revision": int(row["active_revision"]),
            "record_version": int(row["record_version"]),
            "source_fingerprint": row["source_fingerprint"],
            "summary": summary,
            "legacy_record": legacy,
            "linked_run_ids": linked_run_ids,
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
            "last_audit_event_id": row["last_audit_event_id"],
        }
    )


def _row_to_revision(row: sqlite3.Row) -> TaskRetrospectiveRevision:
    """Rehydrate immutable ``TaskRetrospectiveRevision`` from SQLite revision row."""
    raw_legacy = row["legacy_record_json"]
    return TaskRetrospectiveRevision.model_validate(
        {
            "retrospective_id": row["retrospective_id"],
            "task_id": row["task_id"],
            "revision_number": int(row["revision_number"]),
            "source_fingerprint": row["source_fingerprint"],
            "summary": TaskExecutionSummary.model_validate_json(str(row["summary_json"])),
            "legacy_record": (
                ProcessRetrospectiveRecord.model_validate_json(str(raw_legacy))
                if raw_legacy is not None
                else None
            ),
            "recorded_by": row["recorded_by"],
            "recorded_at": row["recorded_at"],
            "audit_event_id": row["audit_event_id"],
        }
    )


class SQLiteProcessRepository:
    """SQLite implementation of ``ProcessRetrospectiveRepository`` and ``ExecutionHistoryReaderPort``."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        """Bind Process retrospective repository to an initialized ``SQLiteRepositoryStore``."""
        self._store = store

    def _append_audit_or_raise(self, conn: sqlite3.Connection, audit_event: Event) -> None:
        """Append audit Event inside current transaction or fail closed for atomic rollback."""
        res = self._store.events.append_on_connection(conn, audit_event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Process retrospective audit event rejected."
            raise RepositoryError("AUDIT_APPEND_FAILED", msg, entity_id=audit_event.task_id)

    def _fetch_linked_run_ids(self, conn: sqlite3.Connection, retrospective_id: str) -> list[str]:
        rows = conn.execute(
            """
            SELECT run_id FROM task_retrospective_runs
            WHERE retrospective_id = ?
            ORDER BY linked_at ASC, run_id ASC;
            """,
            (retrospective_id,),
        ).fetchall()
        return [str(r["run_id"]) for r in rows]

    def _sync_run_links(
        self,
        conn: sqlite3.Connection,
        *,
        retrospective_id: str,
        summary: TaskExecutionSummary,
        linked_at: datetime,
    ) -> None:
        """Link every real Run summarized on ``TaskExecutionSummary`` into ``task_retrospective_runs``."""
        run_status_map = {
            r.run_id: r.status for r in self._store.runs.list_runs_for_task(summary.task_id)
        }
        linked_iso = linked_at.isoformat()
        for rid in summary.run_ids:
            run_status = run_status_map.get(rid, summary.execution_status)
            conn.execute(
                """
                INSERT INTO task_retrospective_runs (
                    retrospective_id, task_id, run_id, run_status, linked_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(retrospective_id, run_id) DO UPDATE SET
                    run_status = excluded.run_status;
                """,
                (retrospective_id, summary.task_id, rid, run_status, linked_iso),
            )

    def upsert_retrospective_atomic(
        self,
        *,
        summary: TaskExecutionSummary,
        legacy_record: ProcessRetrospectiveRecord | None,
        actor_id: str,
        audit_event: Event,
        expected_record_version: int | None = None,
    ) -> tuple[StoredTaskRetrospective, bool, bool]:
        """Atomically persist or deduplicate a versioned Task retrospective inside ``BEGIN IMMEDIATE``."""
        validated_summary = TaskExecutionSummary.model_validate_json(summary.model_dump_json())
        validate_safe_payload(validated_summary.model_dump(mode="json"), "task_execution_summary")
        validated_legacy = (
            ProcessRetrospectiveRecord.model_validate_json(legacy_record.model_dump_json())
            if legacy_record is not None
            else None
        )

        with self._store.transaction_scope() as conn:
            existing = conn.execute(
                "SELECT * FROM task_retrospectives WHERE task_id = ?;",
                (validated_summary.task_id,),
            ).fetchone()

            if existing is None:
                if expected_record_version is not None and expected_record_version != 1:
                    raise StaleRetrospectiveVersionError(
                        "STALE_RETROSPECTIVE_VERSION",
                        f"Expected existing record version {expected_record_version}, but no retrospective exists.",
                    )
                self._append_audit_or_raise(conn, audit_event)
                summary_json = _dumps(validated_summary.model_dump(mode="json"))
                legacy_json = (
                    _dumps(validated_legacy.model_dump(mode="json"))
                    if validated_legacy is not None
                    else None
                )
                created_iso = validated_summary.recorded_at.isoformat()
                updated_iso = validated_summary.updated_at.isoformat()
                try:
                    conn.execute(
                        """
                        INSERT INTO task_retrospectives (
                            retrospective_id, task_id, primary_run_id, execution_status,
                            active_revision, record_version, source_fingerprint,
                            summary_json, legacy_record_json,
                            created_by, created_at, updated_by, updated_at, last_audit_event_id
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            validated_summary.retrospective_id,
                            validated_summary.task_id,
                            validated_summary.run_id,
                            validated_summary.execution_status,
                            validated_summary.retrospective_version,
                            validated_summary.source_fingerprint,
                            summary_json,
                            legacy_json,
                            actor_id,
                            created_iso,
                            actor_id,
                            updated_iso,
                            audit_event.event_id,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO task_retrospective_revisions (
                            retrospective_id, task_id, revision_number, source_fingerprint,
                            summary_json, legacy_record_json, recorded_by, recorded_at, audit_event_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            validated_summary.retrospective_id,
                            validated_summary.task_id,
                            1,
                            validated_summary.source_fingerprint,
                            summary_json,
                            legacy_json,
                            actor_id,
                            created_iso,
                            audit_event.event_id,
                        ),
                    )
                    self._sync_run_links(
                        conn,
                        retrospective_id=validated_summary.retrospective_id,
                        summary=validated_summary,
                        linked_at=validated_summary.updated_at,
                    )
                except sqlite3.IntegrityError as exc:
                    raise DuplicateRetrospectiveConflictError(
                        "DUPLICATE_RETROSPECTIVE_CONFLICT",
                        f"Integrity conflict saving retrospective '{validated_summary.retrospective_id}': {exc}",
                    ) from exc
                except sqlite3.Error as exc:
                    raise RepositoryError(
                        "PERSISTENCE_FAILED",
                        f"Failed storing retrospective '{validated_summary.retrospective_id}': {exc}",
                        entity_id=validated_summary.task_id,
                    ) from exc

                stored_row = conn.execute(
                    "SELECT * FROM task_retrospectives WHERE retrospective_id = ?;",
                    (validated_summary.retrospective_id,),
                ).fetchone()
                assert stored_row is not None
                linked = self._fetch_linked_run_ids(conn, validated_summary.retrospective_id)
                return _row_to_stored_retrospective(stored_row, linked_run_ids=linked), True, True

            # Existing retrospective found for task_id.
            canonical_id = str(existing["retrospective_id"])
            if str(existing["source_fingerprint"]) == validated_summary.source_fingerprint:
                linked = self._fetch_linked_run_ids(conn, canonical_id)
                return _row_to_stored_retrospective(existing, linked_run_ids=linked), False, False

            current_ver = int(existing["record_version"])
            if expected_record_version is not None and current_ver != expected_record_version:
                raise StaleRetrospectiveVersionError(
                    "STALE_RETROSPECTIVE_VERSION",
                    (
                        f"Stale retrospective version for '{validated_summary.task_id}': "
                        f"expected {expected_record_version}, found {current_ver}."
                    ),
                )

            next_rev = int(existing["active_revision"]) + 1
            created_at_dt = datetime.fromisoformat(str(existing["created_at"]))
            normalized_payload = dict(audit_event.redacted_payload)
            normalized_payload["retrospective_id"] = canonical_id
            normalized_payload["retrospective_version"] = next_rev
            normalized_event = audit_event.model_copy(
                update={
                    "redacted_payload": normalized_payload,
                    "correlation_id": f"proc-retro-{canonical_id}-v{next_rev}",
                }
            )
            self._append_audit_or_raise(conn, normalized_event)

            next_summary = validated_summary.model_copy(
                update={
                    "retrospective_id": canonical_id,
                    "retrospective_version": next_rev,
                    "recorded_at": created_at_dt,
                    "audit_event_id": normalized_event.event_id,
                }
            )
            next_legacy = (
                validated_legacy.model_copy(
                    update={
                        "retrospective_id": canonical_id,
                        "recorded_at": created_at_dt,
                        "audit_event_id": normalized_event.event_id,
                    }
                )
                if validated_legacy is not None
                else None
            )
            summary_json = _dumps(next_summary.model_dump(mode="json"))
            legacy_json = (
                _dumps(next_legacy.model_dump(mode="json"))
                if next_legacy is not None
                else None
            )
            updated_iso = next_summary.updated_at.isoformat()
            try:
                cur = conn.execute(
                    """
                    UPDATE task_retrospectives
                    SET primary_run_id = ?,
                        execution_status = ?,
                        active_revision = ?,
                        record_version = record_version + 1,
                        source_fingerprint = ?,
                        summary_json = ?,
                        legacy_record_json = ?,
                        updated_by = ?,
                        updated_at = ?,
                        last_audit_event_id = ?
                    WHERE retrospective_id = ? AND record_version = ?;
                    """,
                    (
                        next_summary.run_id,
                        next_summary.execution_status,
                        next_rev,
                        next_summary.source_fingerprint,
                        summary_json,
                        legacy_json,
                        actor_id,
                        updated_iso,
                        normalized_event.event_id,
                        canonical_id,
                        current_ver,
                    ),
                )
                if cur.rowcount != 1:
                    raise StaleRetrospectiveVersionError(
                        "STALE_RETROSPECTIVE_VERSION",
                        f"Concurrent update conflict for retrospective '{canonical_id}'.",
                    )
                conn.execute(
                    """
                    INSERT INTO task_retrospective_revisions (
                        retrospective_id, task_id, revision_number, source_fingerprint,
                        summary_json, legacy_record_json, recorded_by, recorded_at, audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        canonical_id,
                        next_summary.task_id,
                        next_rev,
                        next_summary.source_fingerprint,
                        summary_json,
                        legacy_json,
                        actor_id,
                        updated_iso,
                        normalized_event.event_id,
                    ),
                )
                self._sync_run_links(
                    conn,
                    retrospective_id=canonical_id,
                    summary=next_summary,
                    linked_at=next_summary.updated_at,
                )
            except StaleRetrospectiveVersionError:
                raise
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed advancing retrospective '{canonical_id}': {exc}",
                    entity_id=next_summary.task_id,
                ) from exc

            updated_row = conn.execute(
                "SELECT * FROM task_retrospectives WHERE retrospective_id = ?;",
                (canonical_id,),
            ).fetchone()
            assert updated_row is not None
            linked = self._fetch_linked_run_ids(conn, canonical_id)
            return _row_to_stored_retrospective(updated_row, linked_run_ids=linked), False, True

    def get_by_task(
        self,
        task_id: str,
        *,
        revision: int | None = None,
    ) -> StoredTaskRetrospective | None:
        """Fetch stored retrospective for ``task_id`` (optionally pinned to ``revision``)."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_retrospectives WHERE task_id = ?;",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            ret_id = str(row["retrospective_id"])
            linked = self._fetch_linked_run_ids(conn, ret_id)
            if revision is not None and revision != int(row["active_revision"]):
                rev_row = conn.execute(
                    """
                    SELECT * FROM task_retrospective_revisions
                    WHERE retrospective_id = ? AND revision_number = ?;
                    """,
                    (ret_id, revision),
                ).fetchone()
                if rev_row is None:
                    return None
                rev = _row_to_revision(rev_row)
                return _row_to_stored_retrospective(
                    row,
                    linked_run_ids=linked,
                    summary_override=rev.summary,
                    legacy_override=rev.legacy_record,
                )
            return _row_to_stored_retrospective(row, linked_run_ids=linked)

    def get_by_retrospective_id(
        self,
        retrospective_id: str,
        *,
        revision: int | None = None,
    ) -> StoredTaskRetrospective | None:
        """Fetch stored retrospective by canonical ``retrospective_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_retrospectives WHERE retrospective_id = ?;",
                (retrospective_id,),
            ).fetchone()
            if row is None:
                return None
            return self.get_by_task(str(row["task_id"]), revision=revision)

    def get_by_run(self, run_id: str) -> StoredTaskRetrospective | None:
        """Fetch the Task retrospective linked to ``run_id`` via ``task_retrospective_runs`` or ``primary_run_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                """
                SELECT r.*
                FROM task_retrospectives r
                LEFT JOIN task_retrospective_runs rl
                    ON rl.retrospective_id = r.retrospective_id
                WHERE r.primary_run_id = ? OR rl.run_id = ?
                LIMIT 1;
                """,
                (run_id, run_id),
            ).fetchone()
            if row is None:
                return None
            linked = self._fetch_linked_run_ids(conn, str(row["retrospective_id"]))
            return _row_to_stored_retrospective(row, linked_run_ids=linked)

    def list_retrospectives(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        execution_status: str | None = None,
        limit: int = 200,
    ) -> list[StoredTaskRetrospective]:
        """List stored Task retrospectives ordered deterministically by ``created_at ASC``."""
        clauses: list[str] = []
        params: list[object] = []
        join_sql = ""
        if run_id is not None:
            join_sql = (
                "LEFT JOIN task_retrospective_runs rl "
                "ON rl.retrospective_id = r.retrospective_id "
            )
            clauses.append("(r.primary_run_id = ? OR rl.run_id = ?)")
            params.extend([run_id, run_id])
        if task_id is not None:
            clauses.append("r.task_id = ?")
            params.append(task_id)
        if execution_status is not None:
            clauses.append("r.execution_status = ?")
            params.append(execution_status)

        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        limit_sql = "LIMIT ?" if limit > 0 else ""
        if limit > 0:
            params.append(limit)

        query = (
            f"SELECT DISTINCT r.* FROM task_retrospectives r "
            f"{join_sql}{where_sql}"
            f"ORDER BY r.created_at ASC, r.retrospective_id ASC {limit_sql};"
        )
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            results: list[StoredTaskRetrospective] = []
            for row in rows:
                linked = self._fetch_linked_run_ids(conn, str(row["retrospective_id"]))
                results.append(_row_to_stored_retrospective(row, linked_run_ids=linked))
            return results

    def list_revisions(self, retrospective_id: str) -> list[TaskRetrospectiveRevision]:
        """List immutable historical revisions ordered ascending by ``revision_number``."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_retrospective_revisions
                WHERE retrospective_id = ?
                ORDER BY revision_number ASC;
                """,
                (retrospective_id,),
            ).fetchall()
            return [_row_to_revision(row) for row in rows]

    def gather_task_execution_bundle(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
    ) -> TaskExecutionEvidenceBundle | None:
        """Assemble real Task, Run, Session, Checklist, Tool, Event, Artifact, and Evidence records."""
        task = self._store.tasks.get_task(task_id)
        if task is None:
            return None

        sources: list[str] = ["tasks_table"]
        runs = self._store.runs.list_runs_for_task(task_id)
        if run_id is not None:
            matched_run = self._store.runs.get_run(run_id)
            if matched_run is None or matched_run.task_id != task_id:
                raise ProcessTaskNotFoundError(
                    "RUN_NOT_FOUND",
                    f"Run '{run_id}' does not exist for task '{task_id}'.",
                )
        if runs:
            sources.append("runs_table")

        sessions = self._store.sessions.list_sessions(task_id=task_id)
        if sessions:
            sources.append("sessions_table")

        stored_ws = self._store.worker_sessions.list_stored_sessions(task_id=task_id)
        worker_steps = [
            WorkerStepSnapshot(
                session_id=ws.session.session_id,
                run_id=ws.binding.run_id,
                agent_id=ws.binding.agent_id,
                step_id=ws.binding.step_id,
                checklist_item_id=ws.binding.checklist_item_id,
                provider_id=ws.binding.provider_id,
                connection_id=ws.binding.connection_id,
                model_id=ws.binding.model_id,
                created_at=ws.binding.created_at,
                updated_at=ws.binding.updated_at,
            )
            for ws in stored_ws
        ]
        if worker_steps:
            sources.append("worker_session_bindings")

        checklist_items = self._store.metadata.list_checklist_items(task_id)
        if checklist_items:
            sources.append("checklist_items")

        tool_executions = self._store.tools.list_executions(task_id=task_id, limit=0)
        if tool_executions:
            sources.append("tool_executions")

        journal_entries = self._store.events.read_entries(task_id=task_id)
        if journal_entries:
            sources.append("event_journal")

        artifacts_by_id: dict[str, TaskArtifactFact] = {}
        with self._store.open_connection() as conn:
            vault_rows = conn.execute(
                """
                SELECT artifact_id, run_id, owner_agent_id, relative_path, department,
                       verification_status, evidence_reference, created_at
                FROM artifact_registry_entries
                WHERE task_id = ? AND lifecycle_status != 'PURGED'
                ORDER BY created_at ASC, artifact_id ASC;
                """,
                (task_id,),
            ).fetchall()
            for r in vault_rows:
                artifacts_by_id[str(r["artifact_id"])] = TaskArtifactFact(
                    artifact_id=str(r["artifact_id"]),
                    run_id=str(r["run_id"]),
                    owner_agent_id=str(r["owner_agent_id"]),
                    relative_path=(
                        scrub_text(str(r["relative_path"]))
                        if r["relative_path"]
                        else None
                    ),
                    department=str(r["department"]) if r["department"] else None,
                    verification_status=str(r["verification_status"]),  # type: ignore[arg-type]
                    evidence_reference=(
                        scrub_text(str(r["evidence_reference"]))
                        if r["evidence_reference"]
                        else None
                    ),
                    created_at=validate_aware_datetime(
                        datetime.fromisoformat(str(r["created_at"])), "created_at"
                    ),
                )
            if vault_rows:
                sources.append("artifact_registry")

            for legacy_art in self._store.metadata.list_artifacts(task_id=task_id):
                if legacy_art.artifact_id not in artifacts_by_id:
                    artifacts_by_id[legacy_art.artifact_id] = TaskArtifactFact(
                        artifact_id=legacy_art.artifact_id,
                        run_id=legacy_art.run_id,
                        owner_agent_id=legacy_art.owner_agent_id,
                        relative_path=scrub_text(legacy_art.relative_path),
                        department=None,
                        verification_status=legacy_art.verification_status,
                        evidence_reference=legacy_art.evidence_reference,
                        created_at=legacy_art.created_at,
                    )
                    if "artifacts_table" not in sources:
                        sources.append("artifacts_table")

            evd_rows = conn.execute(
                """
                SELECT evidence_id, evidence_kind, run_id, artifact_id,
                       source_url, summary, created_at
                FROM evidence_records
                WHERE task_id = ?
                ORDER BY created_at ASC, evidence_id ASC;
                """,
                (task_id,),
            ).fetchall()
            evidence_records = [
                TaskEvidenceFact(
                    evidence_id=str(er["evidence_id"]),
                    evidence_kind=str(er["evidence_kind"]),
                    run_id=str(er["run_id"]) if er["run_id"] else None,
                    artifact_id=str(er["artifact_id"]) if er["artifact_id"] else None,
                    source_url=scrub_text(str(er["source_url"])) if er["source_url"] else None,
                    summary=scrub_text(str(er["summary"])),
                    created_at=validate_aware_datetime(
                        datetime.fromisoformat(str(er["created_at"])), "created_at"
                    ),
                )
                for er in evd_rows
            ]
            if evidence_records:
                sources.append("evidence_vault")

        conflicts = self._store.collaboration.list_conflicts(task_id=task_id)
        resolved_conflicts = [
            ResolvedConflictFact(
                conflict_id=c.conflict_id,
                run_id=c.run_id,
                resolution_note=scrub_text(c.resolution_note or "Conflict resolved"),
                decision_id=c.resolution_decision_id,
                resolved_at=c.resolved_at or c.opened_at,
            )
            for c in conflicts
            if c.status == "RESOLVED" and c.resolution_note
        ]
        if resolved_conflicts:
            sources.append("collaboration_conflicts")

        involved_agent_ids: list[str] = list(
            dict.fromkeys(
                [
                    *task.assigned_agent_ids,
                    *[ws.agent_id for ws in worker_steps],
                    *[s.agent_id for s in sessions],
                    *[a.owner_agent_id for a in artifacts_by_id.values()],
                    *[t.actor_id for t in tool_executions if "." in t.actor_id],
                ]
            )
        )
        agent_snapshots: list[AgentSkillSnapshot] = []
        for aid in involved_agent_ids:
            reg = self._store.agents.get_record(aid)
            if reg is not None:
                agent_snapshots.append(
                    AgentSkillSnapshot(
                        agent_id=reg.agent_id,
                        department=reg.manifest.department,
                        declared_capabilities=list(reg.manifest.capabilities.skills),
                        skills_allowed=list(reg.manifest.skills.allowed),
                    )
                )
        if agent_snapshots:
            sources.append("agent_registry")

        return TaskExecutionEvidenceBundle(
            task=task,
            runs=runs,
            sessions=sessions,
            worker_steps=worker_steps,
            checklist_items=checklist_items,
            tool_executions=tool_executions,
            journal_entries=journal_entries,
            artifacts=list(artifacts_by_id.values()),
            evidence_records=evidence_records,
            resolved_conflicts=resolved_conflicts,
            agent_snapshots=agent_snapshots,
            sources_consulted=sources,
        )

    def _sync_improvement_task_links(
        self,
        conn: sqlite3.Connection,
        *,
        improvement: SuggestedImprovement,
        linked_at: datetime,
    ) -> None:
        linked_iso = linked_at.isoformat()
        for task_id, retro_id in zip(
            improvement.evidence_task_ids,
            improvement.evidence_retrospective_ids,
            strict=True,
        ):
            conn.execute(
                """
                INSERT INTO improvement_task_links (
                    improvement_id, task_id, retrospective_id, linked_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(improvement_id, task_id) DO UPDATE SET
                    retrospective_id = excluded.retrospective_id,
                    linked_at = excluded.linked_at;
                """,
                (improvement.improvement_id, task_id, retro_id, linked_iso),
            )

    def upsert_improvement_atomic(
        self,
        *,
        improvement: SuggestedImprovement,
        actor_id: str,
        audit_event: Event,
    ) -> tuple[SuggestedImprovement, bool, bool]:
        """Atomically persist, update, or deduplicate a P10.02 ``SuggestedImprovement``."""
        validated = SuggestedImprovement.model_validate_json(improvement.model_dump_json())
        validate_safe_payload(validated.model_dump(mode="json"), "suggested_improvement")

        with self._store.transaction_scope() as conn:
            existing = conn.execute(
                """
                SELECT * FROM improvement_suggestions
                WHERE pattern_type = ? AND pattern_key = ?;
                """,
                (validated.pattern_type, validated.pattern_key),
            ).fetchone()

            if existing is None:
                self._append_audit_or_raise(conn, audit_event)
                payload_json = _dumps(validated.model_dump(mode="json"))
                det_iso = validated.detected_at.isoformat()
                upd_iso = validated.updated_at.isoformat()
                try:
                    conn.execute(
                        """
                        INSERT INTO improvement_suggestions (
                            improvement_id, pattern_type, pattern_key, suggested_skill_id,
                            title, status, occurrence_count, record_version,
                            source_fingerprint, improvement_json, detected_by,
                            detected_at, updated_at, last_audit_event_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            validated.improvement_id,
                            validated.pattern_type,
                            validated.pattern_key,
                            validated.suggested_skill_id,
                            validated.title,
                            validated.status,
                            validated.metrics.occurrence_count,
                            validated.source_fingerprint,
                            payload_json,
                            actor_id,
                            det_iso,
                            upd_iso,
                            audit_event.event_id,
                        ),
                    )
                    self._sync_improvement_task_links(
                        conn, improvement=validated, linked_at=validated.updated_at
                    )
                except sqlite3.Error as exc:
                    raise RepositoryError(
                        "PERSISTENCE_FAILED",
                        f"Failed storing improvement '{validated.improvement_id}': {exc}",
                        entity_id=validated.improvement_id,
                    ) from exc
                return validated, True, False

            stored_existing = SuggestedImprovement.model_validate_json(
                str(existing["improvement_json"])
            )
            if str(existing["source_fingerprint"]) == validated.source_fingerprint:
                return stored_existing, False, False

            next_ver = int(existing["record_version"]) + 1
            canonical_id = str(existing["improvement_id"])
            preserved_status = stored_existing.status
            norm_payload = dict(audit_event.redacted_payload)
            norm_payload["improvement_id"] = canonical_id
            norm_payload["record_version"] = next_ver
            norm_event = audit_event.model_copy(update={"redacted_payload": norm_payload})
            self._append_audit_or_raise(conn, norm_event)

            next_imp = validated.model_copy(
                update={
                    "improvement_id": canonical_id,
                    "status": preserved_status,
                    "record_version": next_ver,
                    "detected_at": stored_existing.detected_at,
                    "audit_event_id": norm_event.event_id,
                }
            )
            payload_json = _dumps(next_imp.model_dump(mode="json"))
            upd_iso = next_imp.updated_at.isoformat()
            try:
                conn.execute(
                    """
                    UPDATE improvement_suggestions
                    SET suggested_skill_id = ?,
                        title = ?,
                        occurrence_count = ?,
                        record_version = ?,
                        source_fingerprint = ?,
                        improvement_json = ?,
                        updated_at = ?,
                        last_audit_event_id = ?
                    WHERE improvement_id = ?;
                    """,
                    (
                        next_imp.suggested_skill_id,
                        next_imp.title,
                        next_imp.metrics.occurrence_count,
                        next_ver,
                        next_imp.source_fingerprint,
                        payload_json,
                        upd_iso,
                        norm_event.event_id,
                        canonical_id,
                    ),
                )
                self._sync_improvement_task_links(
                    conn, improvement=next_imp, linked_at=next_imp.updated_at
                )
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed updating improvement '{canonical_id}': {exc}",
                    entity_id=canonical_id,
                ) from exc
            return next_imp, False, True

    def get_improvement(self, improvement_id: str) -> SuggestedImprovement | None:
        """Fetch persisted ``SuggestedImprovement`` by ``improvement_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT improvement_json FROM improvement_suggestions WHERE improvement_id = ?;",
                (improvement_id,),
            ).fetchone()
            if row is None:
                return None
            return SuggestedImprovement.model_validate_json(str(row["improvement_json"]))

    def get_improvement_by_pattern(
        self,
        pattern_type: PatternCategory,
        pattern_key: str,
    ) -> SuggestedImprovement | None:
        """Fetch persisted ``SuggestedImprovement`` by unique ``(pattern_type, pattern_key)``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                """
                SELECT improvement_json FROM improvement_suggestions
                WHERE pattern_type = ? AND pattern_key = ?;
                """,
                (pattern_type, pattern_key),
            ).fetchone()
            if row is None:
                return None
            return SuggestedImprovement.model_validate_json(str(row["improvement_json"]))

    def list_improvements(
        self,
        *,
        pattern_type: PatternCategory | None = None,
        status: ImprovementStatus | None = None,
    ) -> list[SuggestedImprovement]:
        """List persisted improvement suggestions ordered deterministically."""
        clauses: list[str] = []
        params: list[object] = []
        if pattern_type is not None:
            clauses.append("pattern_type = ?")
            params.append(pattern_type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT improvement_json FROM improvement_suggestions
                {where_sql}
                ORDER BY detected_at ASC, improvement_id ASC;
                """,
                tuple(params),
            ).fetchall()
            return [
                SuggestedImprovement.model_validate_json(str(r["improvement_json"])) for r in rows
            ]

    def update_improvement_status(
        self,
        improvement_id: str,
        *,
        status: ImprovementStatus,
        updated_at: datetime,
    ) -> SuggestedImprovement:
        """Transition an existing improvement suggestion's lifecycle status atomically."""
        validated_ts = validate_aware_datetime(updated_at, "updated_at")
        with self._store.transaction_scope() as conn:
            row = conn.execute(
                "SELECT improvement_json, record_version FROM improvement_suggestions WHERE improvement_id = ?;",
                (improvement_id,),
            ).fetchone()
            if row is None:
                raise ProcessValidationError(
                    "IMPROVEMENT_NOT_FOUND",
                    f"Improvement '{improvement_id}' does not exist.",
                )
            current = SuggestedImprovement.model_validate_json(str(row["improvement_json"]))
            next_ver = int(row["record_version"]) + 1
            updated = current.model_copy(
                update={
                    "status": status,
                    "record_version": next_ver,
                    "updated_at": validated_ts,
                }
            )
            conn.execute(
                """
                UPDATE improvement_suggestions
                SET status = ?, record_version = ?, updated_at = ?, improvement_json = ?
                WHERE improvement_id = ?;
                """,
                (
                    status,
                    next_ver,
                    validated_ts.isoformat(),
                    _dumps(updated.model_dump(mode="json")),
                    improvement_id,
                ),
            )
            return updated


__all__ = ["SQLiteProcessRepository"]
