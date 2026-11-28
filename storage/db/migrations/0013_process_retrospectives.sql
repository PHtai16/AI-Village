-- AI Village Schema Migration 0013: Persistent Data-Driven Task Retrospectives (P10.01)
-- Stores one canonical versioned retrospective per Task with trigger-locked immutable revision history
-- and relational run links for restart recovery, deduplication, and Task/Run queries.

CREATE TABLE IF NOT EXISTS task_retrospectives (
    retrospective_id TEXT PRIMARY KEY CHECK (
        retrospective_id GLOB 'ret_*' AND length(trim(retrospective_id)) >= 8
    ),
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE RESTRICT,
    primary_run_id TEXT,
    execution_status TEXT NOT NULL CHECK (length(trim(execution_status)) > 0),
    active_revision INTEGER NOT NULL CHECK (active_revision >= 1),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64),
    summary_json TEXT NOT NULL CHECK (json_valid(summary_json)),
    legacy_record_json TEXT CHECK (
        legacy_record_json IS NULL OR json_valid(legacy_record_json)
    ),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_by TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (primary_run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_task_retrospectives_status_updated
    ON task_retrospectives (execution_status, updated_at DESC);

CREATE TABLE IF NOT EXISTS task_retrospective_revisions (
    retrospective_id TEXT NOT NULL REFERENCES task_retrospectives(retrospective_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64),
    summary_json TEXT NOT NULL CHECK (json_valid(summary_json)),
    legacy_record_json TEXT CHECK (
        legacy_record_json IS NULL OR json_valid(legacy_record_json)
    ),
    recorded_by TEXT NOT NULL CHECK (length(trim(recorded_by)) > 0),
    recorded_at TEXT NOT NULL CHECK (length(trim(recorded_at)) > 0),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    PRIMARY KEY (retrospective_id, revision_number)
);

CREATE INDEX IF NOT EXISTS idx_task_retro_rev_task
    ON task_retrospective_revisions (task_id, revision_number DESC);

CREATE TRIGGER IF NOT EXISTS trg_task_retrospective_revisions_no_update
BEFORE UPDATE ON task_retrospective_revisions
BEGIN
    SELECT RAISE(ABORT, 'task_retrospective_revisions history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_retrospective_revisions_no_delete
BEFORE DELETE ON task_retrospective_revisions
BEGIN
    SELECT RAISE(ABORT, 'task_retrospective_revisions history is immutable and cannot be deleted.');
END;

CREATE TABLE IF NOT EXISTS task_retrospective_runs (
    retrospective_id TEXT NOT NULL REFERENCES task_retrospectives(retrospective_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    run_status TEXT NOT NULL CHECK (length(trim(run_status)) > 0),
    linked_at TEXT NOT NULL CHECK (length(trim(linked_at)) > 0),
    PRIMARY KEY (retrospective_id, run_id),
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_task_retro_runs_run_id
    ON task_retrospective_runs (run_id, task_id);
