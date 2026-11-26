"""SQLite-backed durable Agent Registry repository with immutable version history."""

from __future__ import annotations

import json
import sqlite3

from core.agents.manifest import AgentManifest
from core.agents.registry import (
    AgentManifestVersion,
    AgentRegistryError,
    AgentRegistryRecord,
    CapabilityVerificationEntry,
    DuplicateAgentError,
    StaleAgentVersionError,
)
from core.contracts.event import Event
from storage.repositories.errors import RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore


def _row_to_registry_record(row: sqlite3.Row) -> AgentRegistryRecord:
    """Rehydrate and validate ``AgentRegistryRecord`` from SQLite ``agent_registry`` row."""
    manifest = AgentManifest.model_validate_json(str(row["manifest_json"]))
    cap_entries = [
        CapabilityVerificationEntry.model_validate(item)
        for item in json.loads(str(row["capability_verification_json"]))
    ]
    return AgentRegistryRecord.model_validate(
        {
            "agent_id": row["agent_id"],
            "registration_status": row["registration_status"],
            "active_revision": int(row["active_revision"]),
            "record_version": int(row["record_version"]),
            "manifest": manifest,
            "manifest_sha256": row["manifest_sha256"],
            "source_path": row["source_path"],
            "capability_verification": cap_entries,
            "registered_by": row["registered_by"],
            "registered_at": row["registered_at"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
            "disabled_at": row["disabled_at"],
            "disabled_reason": row["disabled_reason"],
            "last_audit_event_id": row["last_audit_event_id"],
        }
    )


def _row_to_manifest_version(row: sqlite3.Row) -> AgentManifestVersion:
    """Rehydrate and validate ``AgentManifestVersion`` from SQLite ``agent_manifest_versions``."""
    return AgentManifestVersion.model_validate(
        {
            "agent_id": row["agent_id"],
            "manifest_revision": int(row["manifest_revision"]),
            "spec_version": row["spec_version"],
            "manifest": AgentManifest.model_validate_json(str(row["manifest_json"])),
            "manifest_sha256": row["manifest_sha256"],
            "change_summary": row["change_summary"],
            "changed_by": row["changed_by"],
            "changed_at": row["changed_at"],
            "audit_event_id": row["audit_event_id"],
        }
    )


def _serialize_capabilities(entries: list[CapabilityVerificationEntry]) -> str:
    """Serialize capability verification list deterministically to JSON text."""
    return json.dumps(
        [entry.model_dump(mode="json") for entry in entries],
        sort_keys=True,
        separators=(",", ":"),
    )


class SQLiteAgentRegistryRepository:
    """SQLite implementation of ``core.agents.registry.AgentRegistryRepository``."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        """Bind repository to an open ``SQLiteRepositoryStore``."""
        self._store = store

    def create_record(
        self,
        record: AgentRegistryRecord,
        initial_version: AgentManifestVersion,
        audit_event: Event,
    ) -> AgentRegistryRecord:
        """Atomically persist Event, initial Registry record, and revision v1 in one transaction."""
        validated_record = AgentRegistryRecord.model_validate_json(record.model_dump_json())
        validated_version = AgentManifestVersion.model_validate_json(
            initial_version.model_dump_json()
        )
        manifest_json = validated_record.manifest.canonical_json()
        cap_json = _serialize_capabilities(validated_record.capability_verification)

        with self._store.transaction_scope() as conn:
            existing = conn.execute(
                "SELECT 1 FROM agent_registry WHERE agent_id = ?;",
                (validated_record.agent_id,),
            ).fetchone()
            if existing is not None:
                raise DuplicateAgentError(
                    "DUPLICATE_AGENT_ID",
                    f"Agent '{validated_record.agent_id}' is already registered.",
                )

            append_res = self._store.events.append_on_connection(conn, audit_event)
            if append_res.status == "REJECTED" or append_res.entry is None:
                msg = (
                    append_res.error.message
                    if append_res.error
                    else "Audit event append rejected."
                )
                raise AgentRegistryError("AUDIT_APPEND_FAILED", msg)

            try:
                conn.execute(
                    """
                    INSERT INTO agent_registry (
                        agent_id, system_role, department, registration_status,
                        active_revision, record_version, spec_version,
                        manifest_json, manifest_sha256, capability_verification_json,
                        source_path, registered_by, registered_at,
                        updated_by, updated_at, disabled_at, disabled_reason, last_audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        validated_record.agent_id,
                        validated_record.manifest.system_role,
                        validated_record.manifest.department,
                        validated_record.registration_status,
                        validated_record.active_revision,
                        validated_record.manifest.spec_version,
                        manifest_json,
                        validated_record.manifest_sha256,
                        cap_json,
                        validated_record.source_path,
                        validated_record.registered_by,
                        validated_record.registered_at.isoformat(),
                        validated_record.updated_by,
                        validated_record.updated_at.isoformat(),
                        validated_record.disabled_at.isoformat()
                        if validated_record.disabled_at
                        else None,
                        validated_record.disabled_reason,
                        audit_event.event_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO agent_manifest_versions (
                        agent_id, manifest_revision, spec_version, manifest_json,
                        manifest_sha256, change_summary, changed_by, changed_at, audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        validated_version.agent_id,
                        validated_version.manifest_revision,
                        validated_version.spec_version,
                        validated_version.manifest.canonical_json(),
                        validated_version.manifest_sha256,
                        validated_version.change_summary,
                        validated_version.changed_by,
                        validated_version.changed_at.isoformat(),
                        audit_event.event_id,
                    ),
                )
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to persist Agent registration '{validated_record.agent_id}': {exc}",
                    entity_id=validated_record.agent_id,
                ) from exc

            row = conn.execute(
                "SELECT * FROM agent_registry WHERE agent_id = ?;",
                (validated_record.agent_id,),
            ).fetchone()
            assert row is not None
            return _row_to_registry_record(row)

    def get_record(self, agent_id: str) -> AgentRegistryRecord | None:
        """Fetch current Registry record by ``agent_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_registry WHERE agent_id = ?;",
                (agent_id,),
            ).fetchone()
            return _row_to_registry_record(row) if row is not None else None

    def list_records(self, *, include_disabled: bool = True) -> list[AgentRegistryRecord]:
        """List stored Registry records ordered deterministically by ``agent_id``."""
        where_sql = "" if include_disabled else "WHERE registration_status != 'DISABLED' "
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM agent_registry {where_sql}ORDER BY agent_id ASC;"
            ).fetchall()
            return [_row_to_registry_record(row) for row in rows]

    def update_record(
        self,
        record: AgentRegistryRecord,
        *,
        expected_record_version: int,
        new_version: AgentManifestVersion | None,
        audit_event: Event,
    ) -> AgentRegistryRecord:
        """Atomically update Registry state with optimistic locking and optional new revision row."""
        validated_record = AgentRegistryRecord.model_validate_json(record.model_dump_json())
        validated_version = (
            AgentManifestVersion.model_validate_json(new_version.model_dump_json())
            if new_version is not None
            else None
        )
        manifest_json = validated_record.manifest.canonical_json()
        cap_json = _serialize_capabilities(validated_record.capability_verification)

        with self._store.transaction_scope() as conn:
            append_res = self._store.events.append_on_connection(conn, audit_event)
            if append_res.status == "REJECTED" or append_res.entry is None:
                msg = (
                    append_res.error.message
                    if append_res.error
                    else "Audit event append rejected."
                )
                raise AgentRegistryError("AUDIT_APPEND_FAILED", msg)

            cursor = conn.execute(
                """
                UPDATE agent_registry
                SET system_role = ?,
                    department = ?,
                    registration_status = ?,
                    active_revision = ?,
                    record_version = record_version + 1,
                    spec_version = ?,
                    manifest_json = ?,
                    manifest_sha256 = ?,
                    capability_verification_json = ?,
                    source_path = ?,
                    updated_by = ?,
                    updated_at = ?,
                    disabled_at = ?,
                    disabled_reason = ?,
                    last_audit_event_id = ?
                WHERE agent_id = ? AND record_version = ?;
                """,
                (
                    validated_record.manifest.system_role,
                    validated_record.manifest.department,
                    validated_record.registration_status,
                    validated_record.active_revision,
                    validated_record.manifest.spec_version,
                    manifest_json,
                    validated_record.manifest_sha256,
                    cap_json,
                    validated_record.source_path,
                    validated_record.updated_by,
                    validated_record.updated_at.isoformat(),
                    validated_record.disabled_at.isoformat()
                    if validated_record.disabled_at
                    else None,
                    validated_record.disabled_reason,
                    audit_event.event_id,
                    validated_record.agent_id,
                    expected_record_version,
                ),
            )
            if cursor.rowcount != 1:
                existing = conn.execute(
                    "SELECT record_version FROM agent_registry WHERE agent_id = ?;",
                    (validated_record.agent_id,),
                ).fetchone()
                if existing is None:
                    raise AgentRegistryError(
                        "AGENT_NOT_FOUND",
                        f"Agent '{validated_record.agent_id}' does not exist.",
                    )
                raise StaleAgentVersionError(
                    "STALE_RECORD_VERSION",
                    (
                        f"Stale update for '{validated_record.agent_id}': expected "
                        f"record_version={expected_record_version}, found {existing['record_version']}."
                    ),
                )

            if validated_version is not None:
                try:
                    conn.execute(
                        """
                        INSERT INTO agent_manifest_versions (
                            agent_id, manifest_revision, spec_version, manifest_json,
                            manifest_sha256, change_summary, changed_by, changed_at, audit_event_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            validated_version.agent_id,
                            validated_version.manifest_revision,
                            validated_version.spec_version,
                            validated_version.manifest.canonical_json(),
                            validated_version.manifest_sha256,
                            validated_version.change_summary,
                            validated_version.changed_by,
                            validated_version.changed_at.isoformat(),
                            audit_event.event_id,
                        ),
                    )
                except sqlite3.Error as exc:
                    raise RepositoryError(
                        "PERSISTENCE_FAILED",
                        f"Failed to append manifest revision for '{validated_record.agent_id}': {exc}",
                        entity_id=validated_record.agent_id,
                    ) from exc

            row = conn.execute(
                "SELECT * FROM agent_registry WHERE agent_id = ?;",
                (validated_record.agent_id,),
            ).fetchone()
            assert row is not None
            return _row_to_registry_record(row)

    def list_versions(self, agent_id: str) -> list[AgentManifestVersion]:
        """Return immutable historical manifest revisions for ``agent_id`` ordered ascending."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_manifest_versions
                WHERE agent_id = ?
                ORDER BY manifest_revision ASC;
                """,
                (agent_id,),
            ).fetchall()
            return [_row_to_manifest_version(row) for row in rows]

    def get_version(
        self, agent_id: str, manifest_revision: int
    ) -> AgentManifestVersion | None:
        """Fetch an exact historical revision snapshot by ``(agent_id, manifest_revision)``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_manifest_versions
                WHERE agent_id = ? AND manifest_revision = ?;
                """,
                (agent_id, manifest_revision),
            ).fetchone()
            return _row_to_manifest_version(row) if row is not None else None

    def list_active_run_ids_for_agent(self, agent_id: str) -> list[str]:
        """Read-only lookup of non-terminal Runs linked to logical Sessions of ``agent_id``."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT r.run_id
                FROM sessions s
                JOIN runs r ON s.run_id = r.run_id
                WHERE s.agent_id = ?
                  AND r.status IN ('PENDING', 'RUNNING', 'WAITING_INPUT', 'WAITING_APPROVAL')
                ORDER BY r.run_id ASC;
                """,
                (agent_id,),
            ).fetchall()
            return [str(row["run_id"]) for row in rows]
