-- AI Village Schema Migration 0010: Multi-Agent Meetings, Decisions, Conflicts, and Master Delivery
-- Persists P06.01-P06.06 meetings, immutable Master Decision Records, worker conflicts, and final delivery summaries.

CREATE TABLE IF NOT EXISTS meetings (
    meeting_id TEXT PRIMARY KEY CHECK (length(trim(meeting_id)) > 0),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES master_plans(plan_id) ON DELETE RESTRICT,
    team_proposal_id TEXT NOT NULL REFERENCES team_proposals(proposal_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'NEEDS_MASTER_DECISION', 'DECIDED', 'CLOSED')),
    meeting_json TEXT NOT NULL CHECK (json_valid(meeting_json)),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_meetings_task_run ON meetings (task_id, run_id, status);

CREATE TABLE IF NOT EXISTS meeting_decisions (
    decision_id TEXT PRIMARY KEY CHECK (length(trim(decision_id)) > 0),
    meeting_id TEXT REFERENCES meetings(meeting_id) ON DELETE RESTRICT,
    conflict_id TEXT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT,
    decision_json TEXT NOT NULL CHECK (json_valid(decision_json)),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_meeting_decisions_task ON meeting_decisions (task_id, meeting_id);

CREATE TRIGGER IF NOT EXISTS trg_meeting_decisions_no_update
BEFORE UPDATE ON meeting_decisions
BEGIN
    SELECT RAISE(ABORT, 'meeting_decisions history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_meeting_decisions_no_delete
BEFORE DELETE ON meeting_decisions
BEGIN
    SELECT RAISE(ABORT, 'meeting_decisions history is immutable and cannot be deleted.');
END;

CREATE TABLE IF NOT EXISTS worker_conflicts (
    conflict_id TEXT PRIMARY KEY CHECK (length(trim(conflict_id)) > 0),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'RESOLVED_SERIALIZED', 'RESOLVED_DECIDED')),
    conflict_json TEXT NOT NULL CHECK (json_valid(conflict_json)),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    detected_at TEXT NOT NULL CHECK (length(trim(detected_at)) > 0),
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_worker_conflicts_task_run ON worker_conflicts (task_id, run_id, status);

CREATE TABLE IF NOT EXISTS master_deliveries (
    delivery_id TEXT PRIMARY KEY CHECK (length(trim(delivery_id)) > 0),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('VERIFIED', 'NOT_VERIFIED')),
    qa_verified INTEGER NOT NULL CHECK (qa_verified IN (0, 1)),
    delivery_json TEXT NOT NULL CHECK (json_valid(delivery_json)),
    compiled_at TEXT NOT NULL CHECK (length(trim(compiled_at)) > 0),
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_master_deliveries_task ON master_deliveries (task_id, run_id);
