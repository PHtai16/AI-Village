-- AI Village Schema Migration 0008: Evidence-Based Worker Handoffs & Immutable Revisions
-- Persists structured Worker-to-Worker handoffs bound to Task, Run, Plan, Team, Session,
-- verified Artifacts, and Event Journal audit entries without full chat transcripts.

CREATE TABLE IF NOT EXISTS worker_handoffs (
    handoff_id TEXT PRIMARY KEY CHECK (length(trim(handoff_id)) > 0),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES master_plans(plan_id) ON DELETE RESTRICT,
    plan_revision INTEGER NOT NULL CHECK (plan_revision >= 1),
    team_proposal_id TEXT NOT NULL REFERENCES team_proposals(proposal_id) ON DELETE RESTRICT,
    source_step_id TEXT NOT NULL CHECK (length(trim(source_step_id)) > 0),
    target_step_id TEXT NOT NULL CHECK (
        length(trim(target_step_id)) > 0 AND target_step_id != source_step_id
    ),
    from_agent_id TEXT NOT NULL CHECK (length(trim(from_agent_id)) > 0 AND from_agent_id != 'core.master'),
    to_agent_id TEXT NOT NULL CHECK (length(trim(to_agent_id)) > 0 AND to_agent_id != 'core.master'),
    from_session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    to_session_id TEXT REFERENCES sessions(session_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (
        status IN (
            'PROPOSED',
            'VALIDATED',
            'READY',
            'DELIVERED',
            'ACKNOWLEDGED',
            'REJECTED',
            'BLOCKED'
        )
    ),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    handoff_json TEXT NOT NULL CHECK (json_valid(handoff_json)),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    delivered_at TEXT,
    acknowledged_at TEXT,
    rejected_at TEXT,
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_worker_handoffs_task_run
    ON worker_handoffs (task_id, run_id, target_step_id);

CREATE INDEX IF NOT EXISTS idx_worker_handoffs_steps
    ON worker_handoffs (task_id, source_step_id, target_step_id);

CREATE INDEX IF NOT EXISTS idx_worker_handoffs_agents
    ON worker_handoffs (from_agent_id, to_agent_id, status);

CREATE TABLE IF NOT EXISTS worker_handoff_revisions (
    revision_id TEXT PRIMARY KEY CHECK (length(trim(revision_id)) > 0),
    handoff_id TEXT NOT NULL REFERENCES worker_handoffs(handoff_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    record_version INTEGER NOT NULL CHECK (record_version >= 1),
    status TEXT NOT NULL CHECK (
        status IN (
            'PROPOSED',
            'VALIDATED',
            'READY',
            'DELIVERED',
            'ACKNOWLEDGED',
            'REJECTED',
            'BLOCKED'
        )
    ),
    actor_id TEXT NOT NULL CHECK (length(trim(actor_id)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    timestamp TEXT NOT NULL CHECK (length(trim(timestamp)) > 0),
    UNIQUE (handoff_id, record_version),
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_worker_handoff_revisions_handoff
    ON worker_handoff_revisions (handoff_id, record_version ASC);

CREATE TRIGGER IF NOT EXISTS trg_worker_handoff_revisions_no_update
BEFORE UPDATE ON worker_handoff_revisions
BEGIN
    SELECT RAISE(ABORT, 'worker_handoff_revisions history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_worker_handoff_revisions_no_delete
BEFORE DELETE ON worker_handoff_revisions
BEGIN
    SELECT RAISE(ABORT, 'worker_handoff_revisions history is immutable and cannot be deleted.');
END;
