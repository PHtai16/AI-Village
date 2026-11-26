-- AI Village Schema Migration 0005: Structured Master Work Plans & Immutable Revisions
-- Stores Master planning specifications, checklist bindings, and immutable plan revision history.
-- Does not start Runs or fabricate Worker runtime states.

CREATE TABLE IF NOT EXISTS master_plans (
    plan_id TEXT PRIMARY KEY CHECK (length(trim(plan_id)) > 0),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    active_revision INTEGER NOT NULL CHECK (active_revision >= 1),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    status TEXT NOT NULL CHECK (
        status IN (
            'DRAFT',
            'NEEDS_CLARIFICATION',
            'VALIDATED',
            'APPLIED',
            'SUPERSEDED',
            'REJECTED'
        )
    ),
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
    plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_by TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_master_plans_task ON master_plans (task_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_master_plans_status ON master_plans (status);

CREATE TABLE IF NOT EXISTS master_plan_revisions (
    plan_id TEXT NOT NULL REFERENCES master_plans(plan_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    plan_revision INTEGER NOT NULL CHECK (plan_revision >= 1),
    status TEXT NOT NULL CHECK (
        status IN (
            'DRAFT',
            'NEEDS_CLARIFICATION',
            'VALIDATED',
            'APPLIED',
            'SUPERSEDED',
            'REJECTED'
        )
    ),
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
    plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
    change_summary TEXT NOT NULL CHECK (length(trim(change_summary)) > 0),
    changed_by TEXT NOT NULL CHECK (length(trim(changed_by)) > 0),
    changed_at TEXT NOT NULL CHECK (length(trim(changed_at)) > 0),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    PRIMARY KEY (plan_id, plan_revision)
);

CREATE INDEX IF NOT EXISTS idx_master_plan_revisions_task ON master_plan_revisions (task_id, plan_revision);

CREATE TRIGGER IF NOT EXISTS trg_master_plan_revisions_no_update
BEFORE UPDATE ON master_plan_revisions
BEGIN
    SELECT RAISE(ABORT, 'master_plan_revisions history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_master_plan_revisions_no_delete
BEFORE DELETE ON master_plan_revisions
BEGIN
    SELECT RAISE(ABORT, 'master_plan_revisions history is immutable and cannot be deleted.');
END;
