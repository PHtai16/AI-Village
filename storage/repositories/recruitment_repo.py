"""SQLite persistence implementation for P05.07 Agent Recruitment Proposals."""

from __future__ import annotations

import json
import sqlite3

from core.agents.recruitment_models import (
    RecruitmentProposalRecord,
    RecruitmentRevisionRecord,
    RecruitmentStateError,
    RecruitmentStatus,
    RecruitmentNotFoundError,
    StaleRecruitmentVersionError,
    StoredRecruitmentProposal,
)
from core.contracts.event import Event
from storage.repositories.errors import RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore


def _row_to_stored_proposal(row: sqlite3.Row) -> StoredRecruitmentProposal:
    """Rehydrate a validated ``StoredRecruitmentProposal`` from SQLite."""
    record = RecruitmentProposalRecord.model_validate_json(str(row["proposal_json"]))
    return StoredRecruitmentProposal(
        proposal=record,
        record_version=int(row["record_version"]),
        last_audit_event_id=row["last_audit_event_id"],
    )


def _row_to_revision(row: sqlite3.Row) -> RecruitmentRevisionRecord:
    """Rehydrate a validated ``RecruitmentRevisionRecord`` from SQLite."""
    return RecruitmentRevisionRecord.model_validate(
        {
            "revision_id": row["revision_id"],
            "proposal_id": row["proposal_id"],
            "candidate_agent_id": row["candidate_agent_id"],
            "record_version": int(row["record_version"]),
            "status": row["status"],
            "actor_id": row["actor_id"],
            "reason": row["reason"],
            "manifest_sha256": row["manifest_sha256"],
            "event_id": row["event_id"],
            "timestamp": row["timestamp"],
        }
    )


class SQLiteRecruitmentRepository:
    """SQLite implementation of ``core.agents.recruitment_models.RecruitmentRepository``."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        """Bind Recruitment repository to an open ``SQLiteRepositoryStore``."""
        self._store = store

    def _append_audit_or_raise(self, conn: sqlite3.Connection, event: Event) -> None:
        """Append recruitment audit Event inside active SQLite transaction or raise fail-closed."""
        res = self._store.events.append_on_connection(conn, event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Recruitment event append rejected."
            raise RepositoryError("EVENT_APPEND_FAILED", msg, entity_id=event.event_id)

    @staticmethod
    def _insert_revision(conn: sqlite3.Connection, rev: RecruitmentRevisionRecord) -> None:
        """Insert immutable revision snapshot row inside active SQLite transaction."""
        conn.execute(
            """
            INSERT INTO agent_recruitment_revisions (
                revision_id, proposal_id, candidate_agent_id, record_version,
                status, actor_id, reason, manifest_sha256, event_id, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                rev.revision_id,
                rev.proposal_id,
                rev.candidate_agent_id,
                rev.record_version,
                rev.status,
                rev.actor_id,
                rev.reason,
                rev.manifest_sha256,
                rev.event_id,
                rev.timestamp.isoformat(),
            ),
        )

    def create_proposal_atomic(
        self,
        *,
        proposal: RecruitmentProposalRecord,
        revision: RecruitmentRevisionRecord,
        event: Event,
    ) -> StoredRecruitmentProposal:
        """Atomically persist Event, recruitment proposal, and immutable revision v1."""
        validated = RecruitmentProposalRecord.model_validate_json(proposal.model_dump_json())
        rev_row = RecruitmentRevisionRecord.model_validate_json(revision.model_dump_json())
        canonical_json = json.dumps(validated.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

        with self._store.transaction_scope() as conn:
            if conn.execute(
                "SELECT 1 FROM agent_recruitment_proposals WHERE proposal_id = ?;",
                (validated.proposal_id,),
            ).fetchone() is not None:
                raise RecruitmentStateError(
                    "DUPLICATE_PROPOSAL_ID",
                    f"Recruitment proposal '{validated.proposal_id}' already exists.",
                )

            self._append_audit_or_raise(conn, event)
            try:
                conn.execute(
                    """
                    INSERT INTO agent_recruitment_proposals (
                        proposal_id, candidate_agent_id, status, manifest_sha256,
                        task_id, proposed_by, proposal_json, record_version,
                        created_at, evaluated_at, activated_at, rejected_at, last_audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?);
                    """,
                    (
                        validated.proposal_id,
                        validated.candidate_agent_id,
                        validated.status,
                        validated.manifest_sha256,
                        validated.task_id,
                        validated.proposed_by,
                        canonical_json,
                        validated.created_at.isoformat(),
                        validated.evaluated_at.isoformat() if validated.evaluated_at else None,
                        validated.activated_at.isoformat() if validated.activated_at else None,
                        validated.rejected_at.isoformat() if validated.rejected_at else None,
                        event.event_id,
                    ),
                )
                self._insert_revision(conn, rev_row)
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to persist recruitment proposal '{validated.proposal_id}': {exc}",
                    entity_id=validated.proposal_id,
                ) from exc

            row = conn.execute(
                "SELECT * FROM agent_recruitment_proposals WHERE proposal_id = ?;",
                (validated.proposal_id,),
            ).fetchone()
            assert row is not None
            return _row_to_stored_proposal(row)

    def transition_proposal_atomic(
        self,
        *,
        proposal: RecruitmentProposalRecord,
        expected_record_version: int,
        revision: RecruitmentRevisionRecord,
        event: Event,
    ) -> StoredRecruitmentProposal:
        """Atomically transition proposal under optimistic lock and append revision + Event."""
        validated = RecruitmentProposalRecord.model_validate_json(proposal.model_dump_json())
        rev_row = RecruitmentRevisionRecord.model_validate_json(revision.model_dump_json())
        canonical_json = json.dumps(validated.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

        with self._store.transaction_scope() as conn:
            current = conn.execute(
                "SELECT record_version FROM agent_recruitment_proposals WHERE proposal_id = ?;",
                (validated.proposal_id,),
            ).fetchone()
            if current is None:
                raise RecruitmentNotFoundError(
                    "PROPOSAL_NOT_FOUND",
                    f"Recruitment proposal '{validated.proposal_id}' not found.",
                )
            if int(current["record_version"]) != expected_record_version:
                raise StaleRecruitmentVersionError(
                    "STALE_RECRUITMENT_VERSION",
                    f"Expected record_version={expected_record_version}, found {int(current['record_version'])}.",
                )

            self._append_audit_or_raise(conn, event)
            try:
                cursor = conn.execute(
                    """
                    UPDATE agent_recruitment_proposals
                    SET status = ?,
                        proposal_json = ?,
                        record_version = record_version + 1,
                        evaluated_at = ?,
                        activated_at = ?,
                        rejected_at = ?,
                        last_audit_event_id = ?
                    WHERE proposal_id = ? AND record_version = ?;
                    """,
                    (
                        validated.status,
                        canonical_json,
                        validated.evaluated_at.isoformat() if validated.evaluated_at else None,
                        validated.activated_at.isoformat() if validated.activated_at else None,
                        validated.rejected_at.isoformat() if validated.rejected_at else None,
                        event.event_id,
                        validated.proposal_id,
                        expected_record_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleRecruitmentVersionError(
                        "STALE_RECRUITMENT_VERSION",
                        f"Concurrent update detected on proposal '{validated.proposal_id}'.",
                    )
                self._insert_revision(conn, rev_row)
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to transition recruitment proposal '{validated.proposal_id}': {exc}",
                    entity_id=validated.proposal_id,
                ) from exc

            row = conn.execute(
                "SELECT * FROM agent_recruitment_proposals WHERE proposal_id = ?;",
                (validated.proposal_id,),
            ).fetchone()
            assert row is not None
            return _row_to_stored_proposal(row)

    def get_proposal(self, proposal_id: str) -> StoredRecruitmentProposal | None:
        """Load persisted recruitment proposal by identifier or return None."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_recruitment_proposals WHERE proposal_id = ?;",
                (proposal_id,),
            ).fetchone()
            return _row_to_stored_proposal(row) if row is not None else None

    def list_proposals(
        self,
        *,
        candidate_agent_id: str | None = None,
        status: RecruitmentStatus | None = None,
        task_id: str | None = None,
    ) -> list[StoredRecruitmentProposal]:
        """List persisted recruitment proposals filtered cleanly."""
        clauses: list[str] = []
        params: list[object] = []
        if candidate_agent_id is not None:
            clauses.append("candidate_agent_id = ?")
            params.append(candidate_agent_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)

        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = f"SELECT * FROM agent_recruitment_proposals {where_sql}ORDER BY created_at ASC, proposal_id ASC;"
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_stored_proposal(row) for row in rows]

    def list_revisions(self, proposal_id: str) -> list[RecruitmentRevisionRecord]:
        """Return ascending immutable revision history for ``proposal_id``."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_recruitment_revisions
                WHERE proposal_id = ?
                ORDER BY record_version ASC;
                """,
                (proposal_id,),
            ).fetchall()
            return [_row_to_revision(row) for row in rows]
