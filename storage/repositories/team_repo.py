"""SQLite-backed durable Team Proposal repository with atomic Task/Checklist assignment."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import sqlite3

from core.contracts.event import Event
from core.contracts.references import ChecklistItem
from core.contracts.task import Task
from core.orchestration.team_models import (
    StaleTeamProposalError,
    StoredTeamProposal,
    TeamAssignmentResult,
    TeamAssignmentStateError,
    TeamBuilderError,
    TeamProposal,
    TeamProposalNotFoundError,
    TeamProposalRevisionRecord,
)
from core.tasks.state_machine import transition_task
from core.tasks.transitions import ActorTrustKind, TaskTransitionRequest, TransitionContext
from storage.repositories.errors import EntityNotFoundError, RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore


def _row_to_stored_proposal(row: sqlite3.Row) -> StoredTeamProposal:
    """Rehydrate and validate ``StoredTeamProposal`` from SQLite ``team_proposals``."""
    proposal = TeamProposal.model_validate_json(str(row["proposal_json"]))
    return StoredTeamProposal.model_validate(
        {
            "proposal_id": row["proposal_id"],
            "task_id": row["task_id"],
            "plan_id": row["plan_id"],
            "plan_revision": int(row["plan_revision"]),
            "active_revision": int(row["active_revision"]),
            "record_version": int(row["record_version"]),
            "status": row["status"],
            "proposal": proposal,
            "proposal_sha256": row["proposal_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
            "last_audit_event_id": row["last_audit_event_id"],
        }
    )


def _row_to_proposal_revision(row: sqlite3.Row) -> TeamProposalRevisionRecord:
    """Rehydrate and validate ``TeamProposalRevisionRecord`` from ``team_proposal_revisions``."""
    proposal = TeamProposal.model_validate_json(str(row["proposal_json"]))
    return TeamProposalRevisionRecord.model_validate(
        {
            "proposal_id": row["proposal_id"],
            "task_id": row["task_id"],
            "proposal_revision": int(row["proposal_revision"]),
            "status": row["status"],
            "proposal": proposal,
            "proposal_sha256": row["proposal_sha256"],
            "change_summary": row["change_summary"],
            "changed_by": row["changed_by"],
            "changed_at": row["changed_at"],
            "audit_event_id": row["audit_event_id"],
        }
    )


def _to_transition_actor_kind(actor_kind: str) -> ActorTrustKind:
    """Map Governance actor_kind into Core State Machine ActorTrustKind."""
    if actor_kind == "human_operator":
        return "operator"
    if actor_kind == "master_agent":
        return "orchestrator"
    if actor_kind == "runtime_service":
        return "core_runtime"
    if actor_kind == "trusted_verifier":
        return "qa_verifier"
    if actor_kind == "worker_agent":
        return "worker_agent"
    return "governance"


class SQLiteTeamProposalRepository:
    """SQLite implementation of ``core.orchestration.team_models.TeamProposalRepository``."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        """Bind Team Proposal repository to an open ``SQLiteRepositoryStore``."""
        self._store = store

    def _append_audit_or_raise(self, conn: sqlite3.Connection, event: Event) -> None:
        """Append audit Event inside current transaction or abort fail-closed."""
        res = self._store.events.append_on_connection(conn, event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Team audit event append rejected."
            raise TeamBuilderError("AUDIT_APPEND_FAILED", msg)

    def create_proposal(
        self,
        record: StoredTeamProposal,
        initial_revision: TeamProposalRevisionRecord,
        audit_event: Event,
        *,
        extra_events: Sequence[Event] = (),
    ) -> StoredTeamProposal:
        """Atomically persist Event(s), ``team_proposals`` snapshot, and revision v1."""
        validated = StoredTeamProposal.model_validate_json(record.model_dump_json())
        rev_row = TeamProposalRevisionRecord.model_validate_json(
            initial_revision.model_dump_json()
        )

        with self._store.transaction_scope() as conn:
            self._append_audit_or_raise(conn, audit_event)
            for extra_ev in extra_events:
                self._append_audit_or_raise(conn, extra_ev)

            try:
                conn.execute(
                    """
                    INSERT INTO team_proposals (
                        proposal_id, task_id, plan_id, plan_revision, active_revision,
                        record_version, status, proposal_json, proposal_sha256,
                        created_by, created_at, updated_by, updated_at, last_audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        validated.proposal_id,
                        validated.task_id,
                        validated.plan_id,
                        validated.plan_revision,
                        validated.active_revision,
                        validated.status,
                        validated.proposal.canonical_json(),
                        validated.proposal_sha256,
                        validated.created_by,
                        validated.created_at.isoformat(),
                        validated.updated_by,
                        validated.updated_at.isoformat(),
                        audit_event.event_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO team_proposal_revisions (
                        proposal_id, task_id, proposal_revision, status, proposal_json,
                        proposal_sha256, change_summary, changed_by, changed_at, audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        rev_row.proposal_id,
                        rev_row.task_id,
                        rev_row.proposal_revision,
                        rev_row.status,
                        rev_row.proposal.canonical_json(),
                        rev_row.proposal_sha256,
                        rev_row.change_summary,
                        rev_row.changed_by,
                        rev_row.changed_at.isoformat(),
                        audit_event.event_id,
                    ),
                )
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to persist TeamProposal '{validated.proposal_id}': {exc}",
                    entity_id=validated.proposal_id,
                ) from exc

            row = conn.execute(
                "SELECT * FROM team_proposals WHERE proposal_id = ?;",
                (validated.proposal_id,),
            ).fetchone()
            assert row is not None
            return _row_to_stored_proposal(row)

    def revise_proposal(
        self,
        record: StoredTeamProposal,
        new_revision: TeamProposalRevisionRecord,
        *,
        expected_record_version: int,
        audit_event: Event,
        extra_events: Sequence[Event] = (),
    ) -> StoredTeamProposal:
        """Atomically update active proposal under optimistic lock and append revision."""
        validated = StoredTeamProposal.model_validate_json(record.model_dump_json())
        rev_row = TeamProposalRevisionRecord.model_validate_json(
            new_revision.model_dump_json()
        )

        with self._store.transaction_scope() as conn:
            current = conn.execute(
                "SELECT record_version FROM team_proposals WHERE proposal_id = ?;",
                (validated.proposal_id,),
            ).fetchone()
            if current is None:
                raise TeamProposalNotFoundError(
                    "TEAM_PROPOSAL_NOT_FOUND",
                    f"TeamProposal '{validated.proposal_id}' does not exist.",
                )
            if int(current["record_version"]) != expected_record_version:
                raise StaleTeamProposalError(
                    "STALE_PROPOSAL_VERSION",
                    (
                        f"Stale revision for TeamProposal '{validated.proposal_id}': expected "
                        f"record_version={expected_record_version}, found {int(current['record_version'])}."
                    ),
                )

            self._append_audit_or_raise(conn, audit_event)
            for extra_ev in extra_events:
                self._append_audit_or_raise(conn, extra_ev)

            try:
                cursor = conn.execute(
                    """
                    UPDATE team_proposals
                    SET plan_id = ?,
                        plan_revision = ?,
                        active_revision = ?,
                        record_version = record_version + 1,
                        status = ?,
                        proposal_json = ?,
                        proposal_sha256 = ?,
                        updated_by = ?,
                        updated_at = ?,
                        last_audit_event_id = ?
                    WHERE proposal_id = ? AND record_version = ?;
                    """,
                    (
                        validated.plan_id,
                        validated.plan_revision,
                        validated.active_revision,
                        validated.status,
                        validated.proposal.canonical_json(),
                        validated.proposal_sha256,
                        validated.updated_by,
                        validated.updated_at.isoformat(),
                        audit_event.event_id,
                        validated.proposal_id,
                        expected_record_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleTeamProposalError(
                        "STALE_PROPOSAL_VERSION",
                        f"Concurrent modification detected on TeamProposal '{validated.proposal_id}'.",
                    )
                conn.execute(
                    """
                    INSERT INTO team_proposal_revisions (
                        proposal_id, task_id, proposal_revision, status, proposal_json,
                        proposal_sha256, change_summary, changed_by, changed_at, audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        rev_row.proposal_id,
                        rev_row.task_id,
                        rev_row.proposal_revision,
                        rev_row.status,
                        rev_row.proposal.canonical_json(),
                        rev_row.proposal_sha256,
                        rev_row.change_summary,
                        rev_row.changed_by,
                        rev_row.changed_at.isoformat(),
                        audit_event.event_id,
                    ),
                )
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to revise TeamProposal '{validated.proposal_id}': {exc}",
                    entity_id=validated.proposal_id,
                ) from exc

            row = conn.execute(
                "SELECT * FROM team_proposals WHERE proposal_id = ?;",
                (validated.proposal_id,),
            ).fetchone()
            assert row is not None
            return _row_to_stored_proposal(row)

    def get_proposal(self, proposal_id: str) -> StoredTeamProposal | None:
        """Fetch active TeamProposal snapshot by ``proposal_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM team_proposals WHERE proposal_id = ?;",
                (proposal_id,),
            ).fetchone()
            return _row_to_stored_proposal(row) if row is not None else None

    def get_active_proposal_for_task(self, task_id: str) -> StoredTeamProposal | None:
        """Fetch latest TeamProposal bound to ``task_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM team_proposals
                WHERE task_id = ?
                ORDER BY updated_at DESC, record_version DESC, proposal_id ASC
                LIMIT 1;
                """,
                (task_id,),
            ).fetchone()
            return _row_to_stored_proposal(row) if row is not None else None

    def list_proposal_revisions(self, proposal_id: str) -> list[TeamProposalRevisionRecord]:
        """List immutable historical revisions for ``proposal_id`` ordered ascending."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM team_proposal_revisions
                WHERE proposal_id = ?
                ORDER BY proposal_revision ASC;
                """,
                (proposal_id,),
            ).fetchall()
            return [_row_to_proposal_revision(row) for row in rows]

    def apply_team_assignment(
        self,
        *,
        proposal_id: str,
        expected_proposal_record_version: int,
        updated_proposal: StoredTeamProposal,
        new_revision: TeamProposalRevisionRecord,
        assigned_agent_ids: list[str],
        step_agent_map: dict[str, str],
        actor_id: str,
        actor_kind: str,
        timestamp: datetime,
        team_event: Event,
    ) -> TeamAssignmentResult:
        """Atomically assign team onto Task & PENDING ChecklistItems without starting a Run."""
        validated = StoredTeamProposal.model_validate_json(updated_proposal.model_dump_json())
        rev_row = TeamProposalRevisionRecord.model_validate_json(new_revision.model_dump_json())

        with self._store.transaction_scope() as conn:
            current = conn.execute(
                "SELECT record_version FROM team_proposals WHERE proposal_id = ?;",
                (proposal_id,),
            ).fetchone()
            if current is None:
                raise TeamProposalNotFoundError(
                    "TEAM_PROPOSAL_NOT_FOUND",
                    f"TeamProposal '{proposal_id}' does not exist.",
                )
            if int(current["record_version"]) != expected_proposal_record_version:
                raise StaleTeamProposalError(
                    "STALE_PROPOSAL_VERSION",
                    (
                        f"Stale assignment for TeamProposal '{proposal_id}': expected "
                        f"record_version={expected_proposal_record_version}, found {int(current['record_version'])}."
                    ),
                )

            stored_task = self._store.tasks.get_stored_task_on_connection(conn, validated.task_id)
            if stored_task is None:
                raise EntityNotFoundError(
                    f"Task '{validated.task_id}' does not exist.",
                    entity_id=validated.task_id,
                )
            if stored_task.task.status not in {"QUEUED", "ASSIGNED"}:
                raise TeamAssignmentStateError(
                    "TASK_STATE_FORBIDS_ASSIGNMENT",
                    (
                        f"Cannot assign dynamic team while Task '{stored_task.task.task_id}' "
                        f"is in status '{stored_task.task.status}'."
                    ),
                )

            # Update assigned_agent_id on PENDING ChecklistItems without touching status/evidence.
            existing_items = self._store.metadata.list_checklist_items(validated.task_id)
            updated_items: list[ChecklistItem] = []
            for item in existing_items:
                if item.checklist_item_id in step_agent_map:
                    if item.status != "PENDING":
                        raise TeamAssignmentStateError(
                            "CHECKLIST_ITEM_NOT_PENDING",
                            (
                                f"Cannot reassign non-PENDING ChecklistItem "
                                f"'{item.checklist_item_id}' (status={item.status})."
                            ),
                        )
                    next_item = item.model_copy(
                        update={"assigned_agent_id": step_agent_map[item.checklist_item_id]}
                    )
                    updated_items.append(self._store.metadata.save_checklist_item(next_item))
                else:
                    updated_items.append(item)

            audit_event_ids: list[str] = []
            if stored_task.task.status == "QUEUED":
                trans_res = transition_task(
                    TaskTransitionRequest(
                        task=stored_task.task,
                        target_status="ASSIGNED",
                        context=TransitionContext(
                            actor_id=actor_id,
                            actor_kind=_to_transition_actor_kind(actor_kind),
                            timestamp=timestamp,
                            reason=f"Assigned capability-matched team '{proposal_id}' without starting execution.",
                            assigned_agent_ids=assigned_agent_ids,
                            payload={
                                "proposal_id": proposal_id,
                                "plan_id": validated.plan_id,
                            },
                        ),
                    )
                )
                if not trans_res.accepted or trans_res.task is None or trans_res.event is None:
                    err_msg = (
                        trans_res.error.message
                        if trans_res.error
                        else "Task ASSIGNED transition rejected."
                    )
                    raise TeamAssignmentStateError("TASK_ASSIGN_TRANSITION_FAILED", err_msg)
                self._append_audit_or_raise(conn, trans_res.event)
                audit_event_ids.append(trans_res.event.event_id)
                target_task = trans_res.task
            else:
                next_updated_at = timestamp if stored_task.task.created_at is not None else None
                task_data = stored_task.task.model_dump()
                task_data.update(
                    {
                        "assigned_agent_ids": assigned_agent_ids,
                        "updated_at": next_updated_at,
                    }
                )
                target_task = Task.model_validate(task_data)

            self._append_audit_or_raise(conn, team_event)
            audit_event_ids.append(team_event.event_id)

            saved_task = self._store.tasks.save_task_state_on_connection(
                conn,
                target_task,
                expected_version=stored_task.version,
            )

            try:
                cursor = conn.execute(
                    """
                    UPDATE team_proposals
                    SET active_revision = ?,
                        record_version = record_version + 1,
                        status = ?,
                        proposal_json = ?,
                        proposal_sha256 = ?,
                        updated_by = ?,
                        updated_at = ?,
                        last_audit_event_id = ?
                    WHERE proposal_id = ? AND record_version = ?;
                    """,
                    (
                        validated.active_revision,
                        validated.status,
                        validated.proposal.canonical_json(),
                        validated.proposal_sha256,
                        validated.updated_by,
                        validated.updated_at.isoformat(),
                        team_event.event_id,
                        proposal_id,
                        expected_proposal_record_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleTeamProposalError(
                        "STALE_PROPOSAL_VERSION",
                        f"Concurrent modification detected assigning TeamProposal '{proposal_id}'.",
                    )
                conn.execute(
                    """
                    INSERT INTO team_proposal_revisions (
                        proposal_id, task_id, proposal_revision, status, proposal_json,
                        proposal_sha256, change_summary, changed_by, changed_at, audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        rev_row.proposal_id,
                        rev_row.task_id,
                        rev_row.proposal_revision,
                        rev_row.status,
                        rev_row.proposal.canonical_json(),
                        rev_row.proposal_sha256,
                        rev_row.change_summary,
                        rev_row.changed_by,
                        rev_row.changed_at.isoformat(),
                        team_event.event_id,
                    ),
                )
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to finalize assignment for TeamProposal '{proposal_id}': {exc}",
                    entity_id=proposal_id,
                ) from exc

            refreshed_row = conn.execute(
                "SELECT * FROM team_proposals WHERE proposal_id = ?;",
                (proposal_id,),
            ).fetchone()
            assert refreshed_row is not None
            return TeamAssignmentResult(
                stored_proposal=_row_to_stored_proposal(refreshed_row),
                task=saved_task.task,
                checklist_items=updated_items,
                audit_event_ids=audit_event_ids,
            )
