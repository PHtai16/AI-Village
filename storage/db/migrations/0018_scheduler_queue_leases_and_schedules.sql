-- AI Village Schema Migration 0018: Execution Scheduler, Quota Buckets, Queue, Leases & Schedules (P11.03)

CREATE TABLE IF NOT EXISTS scheduler_quota_buckets (
    quota_bucket_key TEXT PRIMARY KEY CHECK (length(trim(quota_bucket_key)) > 0),
    provider_id TEXT NOT NULL CHECK (length(trim(provider_id)) > 0),
    quota_scope_kind TEXT NOT NULL CHECK (
        quota_scope_kind IN (
            'CONNECTION_VERIFIED',
            'PROJECT_SHARED',
            'ACCOUNT_SHARED',
            'ORG_SHARED',
            'UNKNOWN_SHARED'
        )
    ),
    max_concurrency INTEGER NOT NULL CHECK (max_concurrency >= 1),
    rate_limit_max_requests INTEGER CHECK (
        rate_limit_max_requests IS NULL OR rate_limit_max_requests >= 1
    ),
    rate_limit_window_seconds INTEGER CHECK (
        rate_limit_window_seconds IS NULL OR rate_limit_window_seconds >= 1
    ),
    quota_limit_units INTEGER CHECK (quota_limit_units IS NULL OR quota_limit_units >= 0),
    quota_used_units INTEGER NOT NULL DEFAULT 0 CHECK (quota_used_units >= 0),
    base_backoff_seconds INTEGER NOT NULL DEFAULT 5 CHECK (base_backoff_seconds >= 1),
    max_backoff_seconds INTEGER NOT NULL DEFAULT 300 CHECK (max_backoff_seconds >= base_backoff_seconds),
    max_retries INTEGER NOT NULL DEFAULT 3 CHECK (max_retries >= 0),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE TABLE IF NOT EXISTS scheduler_quota_policies (
    policy_id TEXT PRIMARY KEY CHECK (length(trim(policy_id)) > 0),
    provider_id TEXT NOT NULL CHECK (length(trim(provider_id)) > 0),
    connection_id TEXT NOT NULL CHECK (length(trim(connection_id)) > 0),
    project_ref TEXT,
    account_ref TEXT,
    model_id TEXT,
    quota_scope_kind TEXT NOT NULL CHECK (
        quota_scope_kind IN (
            'CONNECTION_VERIFIED',
            'PROJECT_SHARED',
            'ACCOUNT_SHARED',
            'ORG_SHARED',
            'UNKNOWN_SHARED'
        )
    ),
    quota_bucket_key TEXT NOT NULL REFERENCES scheduler_quota_buckets(quota_bucket_key) ON DELETE RESTRICT,
    independent_quota_proof INTEGER NOT NULL DEFAULT 0 CHECK (independent_quota_proof IN (0, 1)),
    max_concurrency INTEGER NOT NULL CHECK (max_concurrency >= 1),
    rate_limit_max_requests INTEGER CHECK (
        rate_limit_max_requests IS NULL OR rate_limit_max_requests >= 1
    ),
    rate_limit_window_seconds INTEGER CHECK (
        rate_limit_window_seconds IS NULL OR rate_limit_window_seconds >= 1
    ),
    quota_limit_units INTEGER CHECK (quota_limit_units IS NULL OR quota_limit_units >= 0),
    base_backoff_seconds INTEGER NOT NULL DEFAULT 5 CHECK (base_backoff_seconds >= 1),
    max_backoff_seconds INTEGER NOT NULL DEFAULT 300 CHECK (max_backoff_seconds >= base_backoff_seconds),
    max_retries INTEGER NOT NULL DEFAULT 3 CHECK (max_retries >= 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    UNIQUE (provider_id, connection_id)
);

CREATE INDEX IF NOT EXISTS idx_sched_policies_bucket
    ON scheduler_quota_policies (quota_bucket_key);

CREATE TABLE IF NOT EXISTS scheduler_queue_entries (
    queue_entry_id TEXT PRIMARY KEY CHECK (
        queue_entry_id GLOB 'qent_*' AND length(trim(queue_entry_id)) >= 9
    ),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    schedule_id TEXT,
    provider_id TEXT NOT NULL CHECK (length(trim(provider_id)) > 0),
    connection_id TEXT NOT NULL CHECK (length(trim(connection_id)) > 0),
    model_id TEXT,
    quota_bucket_key TEXT NOT NULL REFERENCES scheduler_quota_buckets(quota_bucket_key) ON DELETE RESTRICT,
    priority TEXT NOT NULL CHECK (priority IN ('LOW', 'NORMAL', 'HIGH', 'URGENT')),
    priority_rank INTEGER NOT NULL CHECK (priority_rank >= 1),
    fairness_aging_seconds INTEGER NOT NULL DEFAULT 60 CHECK (fairness_aging_seconds >= 1),
    requested_quota_units INTEGER NOT NULL DEFAULT 1 CHECK (requested_quota_units >= 0),
    status TEXT NOT NULL CHECK (
        status IN (
            'QUEUED',
            'READY',
            'RESERVED',
            'RUNNING',
            'BLOCKED_BY_QUOTA',
            'BLOCKED_BY_RATE_LIMIT',
            'BLOCKED_GOVERNANCE',
            'CANCELLED',
            'COMPLETED',
            'FAILED'
        )
    ),
    block_reason_code TEXT,
    block_reason TEXT,
    next_retry_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    idempotency_key TEXT CHECK (idempotency_key IS NULL OR length(trim(idempotency_key)) > 0),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    requested_by TEXT NOT NULL CHECK (length(trim(requested_by)) > 0),
    assigned_worker_id TEXT,
    enqueued_at TEXT NOT NULL CHECK (length(trim(enqueued_at)) > 0),
    reserved_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sched_queue_active_task
    ON scheduler_queue_entries (task_id)
    WHERE status NOT IN ('CANCELLED', 'COMPLETED', 'FAILED');

CREATE UNIQUE INDEX IF NOT EXISTS idx_sched_queue_idempotency
    ON scheduler_queue_entries (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sched_queue_status_order
    ON scheduler_queue_entries (status, priority_rank ASC, enqueued_at ASC);

CREATE TABLE IF NOT EXISTS scheduler_execution_leases (
    lease_id TEXT PRIMARY KEY CHECK (
        lease_id GLOB 'lease_*' AND length(trim(lease_id)) >= 10
    ),
    queue_entry_id TEXT NOT NULL REFERENCES scheduler_queue_entries(queue_entry_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    worker_id TEXT NOT NULL CHECK (length(trim(worker_id)) > 0),
    owner_token TEXT NOT NULL CHECK (length(trim(owner_token)) > 0),
    provider_id TEXT NOT NULL CHECK (length(trim(provider_id)) > 0),
    connection_id TEXT NOT NULL CHECK (length(trim(connection_id)) > 0),
    model_id TEXT,
    quota_bucket_key TEXT NOT NULL REFERENCES scheduler_quota_buckets(quota_bucket_key) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'ACTIVE', 'RELEASED', 'EXPIRED', 'REVOKED')),
    quota_units_reserved INTEGER NOT NULL DEFAULT 1 CHECK (quota_units_reserved >= 0),
    acquired_at TEXT NOT NULL CHECK (length(trim(acquired_at)) > 0),
    expires_at TEXT NOT NULL CHECK (length(trim(expires_at)) > 0),
    started_at TEXT,
    released_at TEXT,
    release_reason TEXT,
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sched_lease_one_active_entry
    ON scheduler_execution_leases (queue_entry_id)
    WHERE state IN ('RESERVED', 'ACTIVE');

CREATE INDEX IF NOT EXISTS idx_sched_lease_bucket_state
    ON scheduler_execution_leases (quota_bucket_key, state, expires_at ASC);

CREATE TABLE IF NOT EXISTS scheduler_rate_limit_events (
    rate_event_id TEXT PRIMARY KEY CHECK (length(trim(rate_event_id)) > 0),
    quota_bucket_key TEXT NOT NULL REFERENCES scheduler_quota_buckets(quota_bucket_key) ON DELETE RESTRICT,
    provider_id TEXT NOT NULL CHECK (length(trim(provider_id)) > 0),
    connection_id TEXT NOT NULL CHECK (length(trim(connection_id)) > 0),
    queue_entry_id TEXT NOT NULL REFERENCES scheduler_queue_entries(queue_entry_id) ON DELETE RESTRICT,
    lease_id TEXT NOT NULL REFERENCES scheduler_execution_leases(lease_id) ON DELETE RESTRICT,
    recorded_at TEXT NOT NULL CHECK (length(trim(recorded_at)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_sched_rate_bucket_ts
    ON scheduler_rate_limit_events (quota_bucket_key, recorded_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_sched_rate_events_no_update
BEFORE UPDATE ON scheduler_rate_limit_events
BEGIN
    SELECT RAISE(ABORT, 'scheduler_rate_limit_events is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_sched_rate_events_no_delete
BEFORE DELETE ON scheduler_rate_limit_events
BEGIN
    SELECT RAISE(ABORT, 'scheduler_rate_limit_events is append-only: DELETE is forbidden');
END;

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    schedule_id TEXT PRIMARY KEY CHECK (
        schedule_id GLOB 'sched_*' AND length(trim(schedule_id)) >= 10
    ),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    project_id TEXT REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    provider_id TEXT NOT NULL CHECK (length(trim(provider_id)) > 0),
    connection_id TEXT NOT NULL CHECK (length(trim(connection_id)) > 0),
    model_id TEXT,
    owner_actor_id TEXT NOT NULL CHECK (length(trim(owner_actor_id)) > 0),
    owner_actor_kind TEXT NOT NULL CHECK (
        owner_actor_kind IN (
            'human_operator', 'master_agent', 'worker_agent', 'runtime_service', 'trusted_verifier'
        )
    ),
    timezone_name TEXT NOT NULL CHECK (length(trim(timezone_name)) > 0),
    scheduled_at TEXT NOT NULL CHECK (length(trim(scheduled_at)) > 0),
    recurrence_rule TEXT,
    recurrence_interval_seconds INTEGER CHECK (
        recurrence_interval_seconds IS NULL OR recurrence_interval_seconds >= 60
    ),
    next_execution_at TEXT,
    previous_execution_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('SCHEDULED', 'TRIGGERED', 'PAUSED', 'COMPLETED', 'CANCELLED')),
    requires_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_approval IN (0, 1)),
    approval_id TEXT,
    policy_note TEXT NOT NULL CHECK (length(trim(policy_note)) > 0),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_sched_tasks_due
    ON scheduled_tasks (status, next_execution_at ASC);
