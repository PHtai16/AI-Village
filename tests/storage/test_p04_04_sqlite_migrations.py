"""Comprehensive SQLite schema, migration, constraint, and transaction tests for Phase 04 / P04.04."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from core.contracts import (
    new_artifact_id,
    new_checklist_item_id,
    new_event_id,
    new_evidence_id,
    new_notification_id,
    new_project_id,
    new_run_id,
    new_session_id,
    new_task_id,
)
from storage.db import (
    DEFAULT_MIGRATIONS_DIR,
    DEFAULT_RUNTIME_DB_PATH,
    MigrationError,
    SQLiteConfig,
    apply_migrations,
    get_latest_migration_version,
    get_schema_version,
    list_applied_migrations,
    open_sqlite_connection,
    resolve_database_path,
    run_integrity_checks,
    split_sql_statements,
    sqlite_connection,
    sqlite_transaction,
    verify_schema_compatibility,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _seed_task(
    conn: sqlite3.Connection,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    status: str = "QUEUED",
    created_at: str | None = "2026-11-25T10:00:00.123456+00:00",
) -> str:
    """Insert a valid baseline row into ``tasks`` for constraint testing."""
    tid = task_id or new_task_id()
    baseline = json.dumps(
        {
            "task_id": tid,
            "project_id": project_id,
            "title": "Verify SQLite schema",
            "description": "Test schema constraints",
            "requested_by": "operator.local",
            "status": status,
            "priority": "NORMAL",
            "created_at": created_at,
        }
    )
    conn.execute(
        """
        INSERT INTO tasks (
            task_id, project_id, title, description, requested_by,
            status, priority, created_at, baseline_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            tid,
            project_id,
            "Verify SQLite schema",
            "Test schema constraints",
            "operator.local",
            status,
            "NORMAL",
            created_at,
            baseline,
        ),
    )
    return tid


def _seed_run(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: str | None = None,
    status: str = "PENDING",
    started_at: str | None = None,
    finished_at: str | None = None,
) -> str:
    """Insert a valid baseline row into ``runs`` for constraint testing."""
    rid = run_id or new_run_id()
    baseline = json.dumps(
        {
            "run_id": rid,
            "task_id": task_id,
            "runtime_id": "custom.master_worker_v1",
            "status": status,
        }
    )
    conn.execute(
        """
        INSERT INTO runs (
            run_id, task_id, runtime_id, status, started_at, finished_at, baseline_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (rid, task_id, "custom.master_worker_v1", status, started_at, finished_at, baseline),
    )
    return rid


def test_import_has_no_disk_side_effects_and_connection_closes(tmp_path: Path) -> None:
    """Importing storage.db must not create `.runtime/ai_village.db` or touch disk."""
    probe_code = (
        "import storage.db; "
        "print(storage.db.resolve_database_path().as_posix())"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("AI_VILLAGE_DB_PATH", None)
    proc = subprocess.run(
        [sys.executable, "-c", probe_code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == ".runtime/ai_village.db"
    assert not (tmp_path / ".runtime").exists()
    assert DEFAULT_RUNTIME_DB_PATH == Path(".runtime") / "ai_village.db"
    assert resolve_database_path(env={"AI_VILLAGE_DB_PATH": str(tmp_path / "custom.db")}) == (
        tmp_path / "custom.db"
    )

    db_file = tmp_path / "sub" / "test.db"
    with sqlite_connection(SQLiteConfig(db_path=db_file)) as conn:
        fk = conn.execute("PRAGMA foreign_keys;").fetchone()
        assert fk is not None and int(fk[0]) == 1
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY);")
        # Leave transaction uncommitted so context manager rolls back cleanly on close.
    assert db_file.exists()

    # Reopen after close to prove lock is released and uncommitted table rolled back.
    with sqlite_connection(db_file) as conn2:
        row = conn2.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'probe';"
        ).fetchone()
        assert row is None


def test_fresh_migration_schema_version_and_idempotency(tmp_path: Path) -> None:
    """Fresh migration creates all expected tables, indexes, triggers, and is idempotent."""
    db_file = tmp_path / "fresh.db"
    latest_version = get_latest_migration_version()
    expected_versions = list(range(1, latest_version + 1))
    with sqlite_connection(db_file) as conn:
        assert get_schema_version(conn) == 0
        first = apply_migrations(conn)
        assert first.previous_version == 0
        assert first.current_version == latest_version
        assert first.applied_versions == expected_versions
        assert first.skipped_versions == []

        report = run_integrity_checks(conn)
        assert report.integrity_ok is True
        assert report.foreign_key_ok is True
        assert report.foreign_keys_enabled is True
        assert report.schema_version == latest_version

        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table';"
            ).fetchall()
        }
        expected_tables = {
            "schema_migrations",
            "projects",
            "connections",
            "tasks",
            "runs",
            "sessions",
            "checklist_items",
            "artifacts",
            "events",
            "notifications",
        }
        assert expected_tables.issubset(tables)

        with sqlite_transaction(conn):
            tid = _seed_task(conn)

        second = apply_migrations(conn)
        assert second.previous_version == latest_version
        assert second.current_version == latest_version
        assert second.applied_versions == []
        assert second.skipped_versions == expected_versions
        kept = conn.execute("SELECT task_id FROM tasks WHERE task_id = ?;", (tid,)).fetchone()
        assert kept is not None and kept["task_id"] == tid
        assert len(list_applied_migrations(conn)) == latest_version


def test_migration_rollback_on_mid_script_failure_and_version_guards(tmp_path: Path) -> None:
    """Mid-script migration failure rolls back partial DDL and preserves schema version and data."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    initial_sql = (DEFAULT_MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
    (mig_dir / "0001_initial.sql").write_text(initial_sql, encoding="utf-8")
    (mig_dir / "0002_broken.sql").write_text(
        """
        CREATE TABLE temp_should_rollback (id INTEGER PRIMARY KEY);
        INSERT INTO nonexistent_table_causes_abort VALUES (1);
        """,
        encoding="utf-8",
    )

    db_file = tmp_path / "rollback.db"
    with sqlite_connection(db_file) as conn:
        res1 = apply_migrations(conn, migrations_dir=mig_dir, target_version=1)
        assert res1.current_version == 1
        with sqlite_transaction(conn):
            tid = _seed_task(conn)

        with pytest.raises(MigrationError) as exc_info:
            apply_migrations(conn, migrations_dir=mig_dir)
        assert exc_info.value.error_code == "MIGRATION_EXECUTION_FAILED"
        assert exc_info.value.version == 2

        assert get_schema_version(conn) == 1
        temp_tbl = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'temp_should_rollback';"
        ).fetchone()
        assert temp_tbl is None
        existing_task = conn.execute(
            "SELECT task_id FROM tasks WHERE task_id = ?;", (tid,)
        ).fetchone()
        assert existing_task is not None

        # Downgrade must be blocked.
        with pytest.raises(MigrationError) as downgrade_exc:
            apply_migrations(conn, migrations_dir=mig_dir, target_version=0)
        assert downgrade_exc.value.error_code == "DOWNGRADE_FORBIDDEN"

    # Tampering with applied migration checksum must fail compatibility check.
    (mig_dir / "0001_initial.sql").write_text(initial_sql + "\n-- tampered\n", encoding="utf-8")
    with sqlite_connection(db_file) as conn:
        with pytest.raises(MigrationError) as checksum_exc:
            verify_schema_compatibility(conn, migrations_dir=mig_dir)
        assert checksum_exc.value.error_code == "CHECKSUM_MISMATCH"

        # Database version ahead of known code migrations must be blocked.
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?);",
            (99, "future_schema", "a" * 64, "2026-11-25T10:00:00+00:00"),
        )
        with pytest.raises(MigrationError) as ahead_exc:
            verify_schema_compatibility(conn, migrations_dir=mig_dir)
        assert ahead_exc.value.error_code == "SCHEMA_VERSION_AHEAD"


def test_foreign_keys_task_run_ownership_and_terminal_checks(tmp_path: Path) -> None:
    """Foreign keys prevent orphan rows and enforce Task A cannot adopt Task B's Run."""
    db_file = tmp_path / "fk.db"
    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)
        with sqlite_transaction(conn):
            t1 = _seed_task(conn, status="RUNNING")
            t2 = _seed_task(conn, status="RUNNING")
            r1 = _seed_run(conn, task_id=t1, status="RUNNING", started_at="2026-11-25T10:01:00+00:00")
            r2 = _seed_run(conn, task_id=t2, status="RUNNING", started_at="2026-11-25T10:01:00+00:00")
            conn.execute("UPDATE tasks SET active_run_id = ? WHERE task_id = ?;", (r1, t1))

        # Task 1 cannot point active_run_id to Run 2 belonging to Task 2.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE tasks SET active_run_id = ? WHERE task_id = ?;", (r2, t1))

        # Terminal Task cannot retain active_run_id.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE tasks SET status = 'COMPLETED' WHERE task_id = ?;",
                (t1,),
            )

        # Orphan Run with missing task_id must fail.
        with pytest.raises(sqlite3.IntegrityError):
            _seed_run(conn, task_id=new_task_id())

        # Orphan ChecklistItem, Artifact, and Notification must fail.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO checklist_items (checklist_item_id, task_id, title, status, order_index)
                VALUES (?, ?, 'step 1', 'PENDING', 0);
                """,
                (new_checklist_item_id(), new_task_id()),
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, task_id, run_id, owner_agent_id, relative_path, verification_status
                ) VALUES (?, ?, ?, 'it.worker', 'output/a.txt', 'UNVERIFIED');
                """,
                (new_artifact_id(), t1, r2),
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO notifications (
                    notification_id, task_id, run_id, event_id, severity, notification_type, created_at
                ) VALUES (?, ?, ?, ?, 'INFO', 'TASK_UPDATE', '2026-11-25T10:02:00+00:00');
                """,
                (new_notification_id(), t1, r1, new_event_id()),
            )


def test_events_append_only_triggers_unique_id_and_sequence_monotonicity(tmp_path: Path) -> None:
    """Events reject duplicate event_id, block SQL UPDATE/DELETE via triggers, and preserve sequence across reconnect."""
    db_file = tmp_path / "events.db"
    evt1 = new_event_id()
    evt2 = new_event_id()
    evt3 = new_event_id()
    tid = new_task_id()

    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)
        with sqlite_transaction(conn):
            conn.execute(
                """
                INSERT INTO events (
                    event_id, timestamp, event_type, source, task_id, redacted_payload_json
                ) VALUES (?, ?, 'task.created', 'core_runtime', ?, '{"step": 1}');
                """,
                (evt1, "2026-11-25T10:00:00.000001+00:00", tid),
            )
            conn.execute(
                """
                INSERT INTO events (
                    event_id, timestamp, event_type, source, task_id, redacted_payload_json
                ) VALUES (?, ?, 'task.status_changed', 'orchestrator', ?, '{"step": 2}');
                """,
                (evt2, "2026-11-25T10:00:01.000002+00:00", tid),
            )

        # Duplicate event_id must fail.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO events (
                    event_id, timestamp, event_type, source, task_id, redacted_payload_json
                ) VALUES (?, ?, 'task.created', 'core_runtime', ?, '{}');
                """,
                (evt1, "2026-11-25T10:00:02+00:00", tid),
            )

        # Missing all attribution anchors must fail CHECK constraint.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO events (
                    event_id, timestamp, event_type, source, redacted_payload_json
                ) VALUES (?, ?, 'runtime.error', 'core_runtime', '{}');
                """,
                (new_event_id(), "2026-11-25T10:00:02+00:00"),
            )

        # Append-only triggers block UPDATE and DELETE.
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(
                "UPDATE events SET redacted_payload_json = '{\"tampered\": true}' WHERE event_id = ?;",
                (evt1,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM events WHERE event_id = ?;", (evt1,))

    # Reconnect and append another event; sequence_number must continue monotonically.
    with sqlite_connection(db_file) as conn2:
        with sqlite_transaction(conn2):
            conn2.execute(
                """
                INSERT INTO events (
                    event_id, timestamp, event_type, source, task_id, redacted_payload_json
                ) VALUES (?, ?, 'task.status_changed', 'core_runtime', ?, '{"step": 3}');
                """,
                (evt3, "2026-11-25T10:00:03+00:00", tid),
            )
        rows = conn2.execute(
            "SELECT sequence_number, event_id FROM events ORDER BY sequence_number ASC;"
        ).fetchall()
        assert [(int(r["sequence_number"]), r["event_id"]) for r in rows] == [
            (1, evt1),
            (2, evt2),
            (3, evt3),
        ]


def test_session_artifact_checklist_notification_connection_constraints(tmp_path: Path) -> None:
    """Validate Session isolation, Artifact/Checklist QA guards, Connection safety, and Notification links."""
    db_file = tmp_path / "domains.db"
    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)
        with sqlite_transaction(conn):
            pid = new_project_id()
            conn.execute(
                "INSERT INTO projects (project_id, name, description) VALUES (?, 'Alpha', 'Grouping');",
                (pid,),
            )
            tid = _seed_task(conn, project_id=pid, status="RUNNING")
            rid = _seed_run(conn, task_id=tid, status="RUNNING", started_at="2026-11-25T10:01:00+00:00")
            sid = new_session_id()
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, legacy_session_ref, agent_id, task_id, run_id,
                    provider_id, connection_id, provider_conversation_id, workspace_reference,
                    status, started_at
                ) VALUES (?, 'sess-legacy-1', 'it.worker', ?, ?, 'antigravity', 'default_antigravity', 'conv-abc-123', 'workspace/t1', 'ACTIVE', '2026-11-25T10:01:01+00:00');
                """,
                (sid, tid, rid),
            )

        # Session cannot reuse session_id as provider_conversation_id.
        with pytest.raises(sqlite3.IntegrityError):
            bad_sid = new_session_id()
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, agent_id, task_id, run_id, provider_conversation_id, status
                ) VALUES (?, 'it.worker', ?, ?, ?, 'INITIALIZED');
                """,
                (bad_sid, tid, rid, bad_sid),
            )

        # Artifact cannot claim VERIFIED_PASS without sha256 and evidence_reference.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, task_id, run_id, owner_agent_id, relative_path,
                    verification_status, evidence_reference, sha256
                ) VALUES (?, ?, ?, 'it.worker', 'output/proof.txt', 'VERIFIED_PASS', 'evd_1234', NULL);
                """,
                (new_artifact_id(), tid, rid),
            )

        # ChecklistItem cannot claim COMPLETED without evidence_reference and completed_at.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO checklist_items (
                    checklist_item_id, task_id, title, status, order_index, evidence_reference, completed_at
                ) VALUES (?, ?, 'Check disk', 'COMPLETED', 0, NULL, '2026-11-25T10:05:00+00:00');
                """,
                (new_checklist_item_id(), tid),
            )

        # Connection metadata rejects raw secret handles or '=' assignments.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO connections (provider_id, connection_id, credential_ref)
                VALUES ('google_gemini', 'primary', 'AIzaFakeSecretKeyValue1234567890');
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO connections (provider_id, connection_id, credential_ref)
                VALUES ('google_gemini', 'primary', 'api_key=bad');
                """
            )

        # Valid Connection metadata + Event + Notification succeed.
        ev_id = new_event_id()
        with sqlite_transaction(conn):
            conn.execute(
                """
                INSERT INTO connections (provider_id, connection_id, model_id, project_ref, credential_ref)
                VALUES ('google_gemini', 'gemini_primary', 'gemini-2.5-pro', 'prj_billing_1', 'env:GEMINI_API_KEY');
                """
            )
            conn.execute(
                """
                INSERT INTO events (
                    event_id, timestamp, event_type, source, task_id, run_id, session_id, redacted_payload_json
                ) VALUES (?, '2026-11-25T10:02:00+00:00', 'notification.created', 'core_runtime', ?, ?, ?, '{}');
                """,
                (ev_id, tid, rid, sid),
            )
            conn.execute(
                """
                INSERT INTO notifications (
                    notification_id, task_id, run_id, event_id, severity, notification_type, created_at
                ) VALUES (?, ?, ?, ?, 'INFO', 'TASK_UPDATE', '2026-11-25T10:02:00+00:00');
                """,
                (new_notification_id(), tid, rid, ev_id),
            )


def test_nullable_telemetry_json_timestamp_precision_and_transaction_rollback(tmp_path: Path) -> None:
    """Telemetry NULLs stay NULL (never 0), timestamps keep subsecond offsets, and rollbacks undo multi-statement writes."""
    db_file = tmp_path / "telemetry_tx.db"
    ts_offset = datetime(2026, 11, 25, 10, 15, 30, 654321, tzinfo=timezone(timedelta(hours=7)))
    iso_ts = ts_offset.isoformat()

    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)
        with sqlite_transaction(conn):
            tid = _seed_task(conn, created_at=iso_ts)
            rid = _seed_run(conn, task_id=tid, status="RUNNING", started_at=iso_ts)
            conn.execute(
                """
                UPDATE runs
                SET telemetry_status = 'UNAVAILABLE_FROM_PROVIDER',
                    telemetry_source = 'unavailable'
                WHERE run_id = ?;
                """,
                (rid,),
            )

        row = conn.execute(
            """
            SELECT started_at, telemetry_input_tokens, telemetry_total_tokens,
                   telemetry_actual_cost_usd, telemetry_status, telemetry_source
            FROM runs WHERE run_id = ?;
            """,
            (rid,),
        ).fetchone()
        assert row is not None
        assert row["started_at"] == iso_ts
        assert datetime.fromisoformat(row["started_at"]) == ts_offset
        assert row["telemetry_input_tokens"] is None
        assert row["telemetry_total_tokens"] is None
        assert row["telemetry_actual_cost_usd"] is None
        assert row["telemetry_status"] == "UNAVAILABLE_FROM_PROVIDER"
        assert row["telemetry_source"] == "unavailable"

        # Multi-statement transaction rollback on second statement failure.
        bad_tid = new_task_id()
        with pytest.raises(sqlite3.IntegrityError):
            with sqlite_transaction(conn):
                _seed_task(conn, task_id=bad_tid, created_at=iso_ts)
                # Violate telemetry VERIFIED_MEASURED + unavailable CHECK constraint.
                conn.execute(
                    """
                    UPDATE runs
                    SET telemetry_status = 'VERIFIED_MEASURED',
                        telemetry_source = 'unavailable'
                    WHERE run_id = ?;
                    """,
                    (rid,),
                )
        missing_task = conn.execute(
            "SELECT task_id FROM tasks WHERE task_id = ?;", (bad_tid,)
        ).fetchone()
        assert missing_task is None

        report = run_integrity_checks(conn)
        assert report.integrity_ok is True
        assert report.foreign_key_ok is True
        assert split_sql_statements("-- comment\nSELECT 1;\n") == ["SELECT 1;"]
