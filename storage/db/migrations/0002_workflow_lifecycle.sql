-- AI Village Schema Migration 0002: Workflow Idempotency & Owned Execution Tracking
-- Adds durable idempotency records and process-execution ownership metadata for P04.06.

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL CHECK (length(trim(scope)) > 0),
    idempotency_key TEXT NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    execution_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMMITTED', 'REJECTED')),
    response_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(response_json)),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    PRIMARY KEY (scope, idempotency_key),
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS run_executions (
    execution_id TEXT PRIMARY KEY CHECK (length(trim(execution_id)) > 0),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    owner_token TEXT NOT NULL CHECK (length(trim(owner_token)) > 0),
    pid INTEGER CHECK (pid IS NULL OR pid > 0),
    lifecycle_state TEXT NOT NULL CHECK (
        lifecycle_state IN (
            'REGISTERED',
            'RUNNING',
            'CANCEL_REQUESTED',
            'CANCELLING',
            'CANCEL_CONFIRMED',
            'TIMED_OUT',
            'SUCCEEDED',
            'FAILED',
            'UNOBSERVABLE'
        )
    ),
    cancel_requested_at TEXT,
    timeout_deadline_at TEXT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_idempotency_task_run ON idempotency_keys (task_id, run_id);
CREATE INDEX IF NOT EXISTS idx_run_executions_task_run ON run_executions (task_id, run_id);
CREATE INDEX IF NOT EXISTS idx_run_executions_state ON run_executions (lifecycle_state);
