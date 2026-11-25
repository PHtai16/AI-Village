"""SQLite-backed durable repository for Human Approval Gate records and single-use consumption."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from typing import Callable, TypeVar

from core.contracts.event import Event
from core.governance.approvals import ApprovalError
from core.governance.models import ApprovalPolicyContext, ApprovalRequest
from storage.db.connection import sqlite_transaction
from storage.repositories.errors import DuplicateEntityError, EntityNotFoundError, StaleStateError
from storage.repositories.sqlite_store import SQLiteRepositoryStore

T = TypeVar("T")


def _row_to_approval(row: sqlite3.Row) -> ApprovalRequest:
    """Rehydrate a validated ``ApprovalRequest`` from SQLite ``approval_requests``."""
    return ApprovalRequest.model_validate(
        {
            "approval_id": row["approval_id"],
            "requester_id": row["requester_id"],
            "requester_kind": row["requester_kind"],
            "action": row["action"],
            "target_resource": row["target_resource"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "request_fingerprint": row["request_fingerprint"],
            "policy_snapshot": json.loads(str(row["policy_snapshot_json"])),
            "risk_summary": row["risk_summary"],
            "status": row["status"],
            "requested_at": row["requested_at"],
            "expires_at": row["expires_at"],
            "decided_at": row["decided_at"],
            "decided_by": row["decided_by"],
            "decision_reason": row["decision_reason"],
            "consumed_at": row["consumed_at"],
            "request_event_id": row["request_event_id"],
            "decision_event_id": row["decision_event_id"],
            "consumed_event_id": row["consumed_event_id"],
            "version": int(row["version"]),
        }
    )


class SQLiteApprovalRepository:
    """SQLite implementation of ``core.governance.approvals.ApprovalRepository``."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        self._store = store

    def create_approval(self, approval: ApprovalRequest) -> ApprovalRequest:
        """Persist a new PENDING ``ApprovalRequest``."""
        validated = ApprovalRequest.model_validate_json(approval.model_dump_json())
        snapshot_json = validated.policy_snapshot.model_dump_json()
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                existing = conn.execute(
                    "SELECT 1 FROM approval_requests WHERE approval_id = ?;",
                    (validated.approval_id,),
                ).fetchone()
                if existing is not None:
                    raise DuplicateEntityError(
                        f"Approval '{validated.approval_id}' already exists.",
                        entity_id=validated.approval_id,
                    )
                conn.execute(
                    """
                    INSERT INTO approval_requests (
                        approval_id, requester_id, requester_kind, action, target_resource,
                        task_id, run_id, request_fingerprint, policy_snapshot_json,
                        risk_summary, status, requested_at, expires_at,
                        decided_at, decided_by, decision_reason, consumed_at,
                        request_event_id, decision_event_id, consumed_event_id, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1);
                    """,
                    (
                        validated.approval_id,
                        validated.requester_id,
                        validated.requester_kind,
                        validated.action,
                        validated.target_resource,
                        validated.task_id,
                        validated.run_id,
                        validated.request_fingerprint,
                        snapshot_json,
                        validated.risk_summary,
                        validated.status,
                        validated.requested_at.isoformat(),
                        validated.expires_at.isoformat() if validated.expires_at else None,
                        validated.decided_at.isoformat() if validated.decided_at else None,
                        validated.decided_by,
                        validated.decision_reason,
                        validated.consumed_at.isoformat() if validated.consumed_at else None,
                        validated.request_event_id,
                        validated.decision_event_id,
                        validated.consumed_event_id,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM approval_requests WHERE approval_id = ?;",
                    (validated.approval_id,),
                ).fetchone()
                assert row is not None
                return _row_to_approval(row)

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        """Load an ``ApprovalRequest`` by identifier."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?;",
                (approval_id,),
            ).fetchone()
            return _row_to_approval(row) if row is not None else None

    def update_decision(
        self,
        approval: ApprovalRequest,
        *,
        expected_version: int,
    ) -> ApprovalRequest:
        """Persist APPROVED, REJECTED, or EXPIRED under optimistic locking."""
        validated = ApprovalRequest.model_validate_json(approval.model_dump_json())
        allowed_prior_sql = (
            "('PENDING', 'APPROVED')" if validated.status == "EXPIRED" else "('PENDING')"
        )
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                cursor = conn.execute(
                    f"""
                    UPDATE approval_requests
                    SET status = ?,
                        decided_at = ?,
                        decided_by = ?,
                        decision_reason = ?,
                        decision_event_id = ?,
                        version = version + 1
                    WHERE approval_id = ? AND version = ? AND status IN {allowed_prior_sql};
                    """,
                    (
                        validated.status,
                        validated.decided_at.isoformat() if validated.decided_at else None,
                        validated.decided_by,
                        validated.decision_reason,
                        validated.decision_event_id,
                        validated.approval_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    existing = conn.execute(
                        "SELECT version, status FROM approval_requests WHERE approval_id = ?;",
                        (validated.approval_id,),
                    ).fetchone()
                    if existing is None:
                        raise EntityNotFoundError(
                            f"Approval '{validated.approval_id}' does not exist.",
                            entity_id=validated.approval_id,
                        )
                    raise StaleStateError(
                        (
                            f"Stale approval update for '{validated.approval_id}': expected v{expected_version} PENDING, "
                            f"found v{existing['version']} {existing['status']}."
                        ),
                        entity_id=validated.approval_id,
                    )
                row = conn.execute(
                    "SELECT * FROM approval_requests WHERE approval_id = ?;",
                    (validated.approval_id,),
                ).fetchone()
                assert row is not None
                return _row_to_approval(row)

    def consume_and_execute(
        self,
        *,
        approval_id: str,
        expected_version: int,
        consumed_at: datetime,
        consumed_event: Event,
        action_fn: Callable[[], T],
    ) -> tuple[ApprovalRequest, T]:
        """Atomically mark APPROVED -> CONSUMED, append audit Event, and execute ``action_fn()`` or rollback."""
        with self._store.transaction_scope() as conn:
            append_res = self._store.events.append_on_connection(conn, consumed_event)
            if append_res.status == "REJECTED" or append_res.entry is None:
                raise ApprovalError(
                    "AUDIT_APPEND_FAILED",
                    append_res.error.message if append_res.error else "Failed to audit approval consumption.",
                )

            cursor = conn.execute(
                """
                UPDATE approval_requests
                SET status = 'CONSUMED',
                    consumed_at = ?,
                    consumed_event_id = ?,
                    version = version + 1
                WHERE approval_id = ? AND version = ? AND status = 'APPROVED';
                """,
                (
                    consumed_at.isoformat(),
                    consumed_event.event_id,
                    approval_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ApprovalError(
                    "APPROVAL_ALREADY_CONSUMED",
                    f"Approval '{approval_id}' could not be consumed (stale version or non-APPROVED state).",
                )

            # Execute protected action inside same transaction scope; exception triggers full SQLite rollback.
            result = action_fn()
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?;",
                (approval_id,),
            ).fetchone()
            assert row is not None
            return _row_to_approval(row), result
