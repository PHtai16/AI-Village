-- AI Village Schema Migration 0007: Worker Session Bindings & Transition Audit History
-- Extends logical `sessions` with pinned Manifest revision bindings, isolation namespaces,
-- idempotency tracking, reconciliation state, and append-only transition records.

CREATE TABLE IF NOT EXISTS worker_session_bindings (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    agent_id TEXT NOT NULL CHECK (length(trim(agent_id)) > 0 AND agent_id != 'core.master'),
    plan_id TEXT NOT NULL REFERENCES master_plans(plan_id) ON DELETE RESTRICT,
    plan_revision INTEGER NOT NULL CHECK (plan_revision >= 1),
    team_proposal_id TEXT NOT NULL REFERENCES team_proposals(proposal_id) ON DELETE RESTRICT,
    step_id TEXT NOT NULL CHECK (length(trim(step_id)) > 0),
    checklist_item_id TEXT REFERENCES checklist_items(checklist_item_id) ON DELETE RESTRICT,
    pinned_manifest_revision INTEGER NOT NULL CHECK (pinned_manifest_revision >= 1),
    pinned_manifest_sha256 TEXT NOT NULL CHECK (length(pinned_manifest_sha256) = 64),
    provider_id TEXT NOT NULL CHECK (length(trim(provider_id)) > 0),
    connection_id TEXT,
    model_id TEXT,
    workspace_scope TEXT NOT NULL CHECK (length(trim(workspace_scope)) > 0),
    private_context_namespace TEXT NOT NULL UNIQUE CHECK (length(trim(private_context_namespace)) > 0),
    idempotency_key TEXT,
    request_fingerprint TEXT CHECK (request_fingerprint IS NULL OR length(request_fingerprint) = 64),
    execution_id TEXT,
    correlation_id TEXT NOT NULL CHECK (length(trim(correlation_id)) > 0),
    reconciliation_status TEXT NOT NULL DEFAULT 'NONE' CHECK (
        reconciliation_status IN ('NONE', 'NEEDS_RECONCILIATION', 'RECONCILED')
    ),
    reconciliation_reason TEXT,
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    last_audit_event_id TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_session_idempotency
    ON worker_session_bindings (task_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_worker_sessions_task_run_agent
    ON worker_session_bindings (task_id, run_id, agent_id);

CREATE INDEX IF NOT EXISTS idx_worker_sessions_task_step
    ON worker_session_bindings (task_id, step_id);

CREATE INDEX IF NOT EXISTS idx_sessions_provider_conversation
    ON sessions (provider_id, provider_conversation_id)
    WHERE provider_conversation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS worker_session_transitions (
    transition_id TEXT PRIMARY KEY CHECK (length(trim(transition_id)) > 0),
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    from_status TEXT CHECK (
        from_status IS NULL
        OR from_status IN ('INITIALIZED', 'ACTIVE', 'IDLE', 'CLOSED', 'TERMINATED', 'FAILED')
    ),
    to_status TEXT NOT NULL CHECK (
        to_status IN ('INITIALIZED', 'ACTIVE', 'IDLE', 'CLOSED', 'TERMINATED', 'FAILED')
    ),
    actor_id TEXT NOT NULL CHECK (length(trim(actor_id)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    evidence_summary TEXT NOT NULL CHECK (length(trim(evidence_summary)) > 0),
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    timestamp TEXT NOT NULL CHECK (length(trim(timestamp)) > 0),
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_worker_session_transitions_session
    ON worker_session_transitions (session_id, timestamp ASC);

CREATE TRIGGER IF NOT EXISTS trg_worker_session_transitions_no_update
BEFORE UPDATE ON worker_session_transitions
BEGIN
    SELECT RAISE(ABORT, 'worker_session_transitions history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_worker_session_transitions_no_delete
BEFORE DELETE ON worker_session_transitions
BEGIN
    SELECT RAISE(ABORT, 'worker_session_transitions history is immutable and cannot be deleted.');
END;
