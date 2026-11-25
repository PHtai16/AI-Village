-- AI Village Schema Migration 0003: Governance & Human Approval Gate Persistence
-- Adds durable approval_requests table with single-use consumption tracking and event links.

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id TEXT PRIMARY KEY CHECK (length(trim(approval_id)) > 0),
    requester_id TEXT NOT NULL CHECK (length(trim(requester_id)) > 0),
    requester_kind TEXT NOT NULL CHECK (
        requester_kind IN (
            'human_operator',
            'master_agent',
            'worker_agent',
            'runtime_service',
            'trusted_verifier'
        )
    ),
    action TEXT NOT NULL CHECK (length(trim(action)) > 0),
    target_resource TEXT NOT NULL CHECK (length(trim(target_resource)) > 0),
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    policy_snapshot_json TEXT NOT NULL CHECK (json_valid(policy_snapshot_json)),
    risk_summary TEXT NOT NULL CHECK (length(trim(risk_summary)) > 0),
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'CONSUMED')
    ),
    requested_at TEXT NOT NULL CHECK (length(trim(requested_at)) > 0),
    expires_at TEXT,
    decided_at TEXT,
    decided_by TEXT,
    decision_reason TEXT,
    consumed_at TEXT,
    request_event_id TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    decision_event_id TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    consumed_event_id TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approval_requests (status);
CREATE INDEX IF NOT EXISTS idx_approvals_task_run ON approval_requests (task_id, run_id);
CREATE INDEX IF NOT EXISTS idx_approvals_fingerprint ON approval_requests (request_fingerprint);
