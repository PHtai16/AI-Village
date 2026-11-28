-- AI Village Schema Migration 0019: Persistent Resource Ledger, Budget Policies & Threshold Alerts (P11.04)

CREATE TABLE IF NOT EXISTS resource_budget_policies (
    policy_id TEXT PRIMARY KEY CHECK (
        policy_id GLOB 'rpol_*' AND length(trim(policy_id)) >= 9
    ),
    scope_kind TEXT NOT NULL CHECK (
        scope_kind IN ('TASK', 'PROJECT', 'CONNECTION', 'QUOTA_BUCKET', 'PROVIDER_MODEL')
    ),
    scope_ref TEXT NOT NULL CHECK (length(trim(scope_ref)) > 0),
    provider_id TEXT,
    connection_id TEXT,
    model_id TEXT,
    project_ref TEXT,
    account_ref TEXT,
    quota_scope_kind TEXT NOT NULL DEFAULT 'UNKNOWN_SHARED' CHECK (
        quota_scope_kind IN (
            'CONNECTION_VERIFIED',
            'PROJECT_SHARED',
            'ACCOUNT_SHARED',
            'ORG_SHARED',
            'UNKNOWN_SHARED'
        )
    ),
    quota_bucket_key TEXT,
    independent_quota_proof INTEGER NOT NULL DEFAULT 0 CHECK (independent_quota_proof IN (0, 1)),
    budget_cost_usd_limit REAL CHECK (budget_cost_usd_limit IS NULL OR budget_cost_usd_limit >= 0),
    duration_ms_limit INTEGER CHECK (duration_ms_limit IS NULL OR duration_ms_limit >= 0),
    memory_mb_limit REAL CHECK (memory_mb_limit IS NULL OR memory_mb_limit >= 0),
    total_tokens_limit INTEGER CHECK (total_tokens_limit IS NULL OR total_tokens_limit >= 0),
    quota_units_limit INTEGER CHECK (quota_units_limit IS NULL OR quota_units_limit >= 0),
    warning_ratio REAL NOT NULL DEFAULT 0.8 CHECK (warning_ratio > 0 AND warning_ratio < 1.0),
    critical_ratio REAL NOT NULL DEFAULT 1.0 CHECK (critical_ratio > warning_ratio AND critical_ratio <= 1.0),
    requires_operator_approval_above_critical INTEGER NOT NULL DEFAULT 1 CHECK (
        requires_operator_approval_above_critical IN (0, 1)
    ),
    approval_id TEXT,
    policy_note TEXT NOT NULL CHECK (length(trim(policy_note)) > 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    UNIQUE (scope_kind, scope_ref)
);

CREATE INDEX IF NOT EXISTS idx_resource_policies_bucket
    ON resource_budget_policies (quota_bucket_key, enabled);

CREATE TABLE IF NOT EXISTS resource_ledger_entries (
    ledger_entry_id TEXT PRIMARY KEY CHECK (
        ledger_entry_id GLOB 'rled_*' AND length(trim(ledger_entry_id)) >= 9
    ),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    project_id TEXT REFERENCES projects(project_id) ON DELETE RESTRICT,
    agent_id TEXT,
    session_id TEXT,
    provider_id TEXT NOT NULL CHECK (length(trim(provider_id)) > 0),
    connection_id TEXT,
    model_id TEXT,
    project_ref TEXT,
    account_ref TEXT,
    quota_scope_kind TEXT NOT NULL CHECK (
        quota_scope_kind IN (
            'CONNECTION_VERIFIED',
            'PROJECT_SHARED',
            'ACCOUNT_SHARED',
            'ORG_SHARED',
            'UNKNOWN_SHARED'
        )
    ),
    quota_bucket_key TEXT NOT NULL CHECK (length(trim(quota_bucket_key)) > 0),
    quota_independence_proven INTEGER NOT NULL DEFAULT 0 CHECK (quota_independence_proven IN (0, 1)),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    provider_duration_ms INTEGER CHECK (provider_duration_ms IS NULL OR provider_duration_ms >= 0),
    duration_source TEXT NOT NULL CHECK (
        duration_source IN ('PROVIDER_MEASURED', 'ADAPTER_MEASURED', 'HOST_SAMPLED', 'UNAVAILABLE')
    ),
    memory_mb REAL CHECK (memory_mb IS NULL OR memory_mb >= 0),
    peak_memory_mb REAL CHECK (peak_memory_mb IS NULL OR peak_memory_mb >= 0),
    memory_source TEXT NOT NULL CHECK (
        memory_source IN ('HOST_SAMPLED', 'CONTAINER_MEASURED', 'PROVIDER_REPORTED', 'UNAVAILABLE')
    ),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    thinking_tokens INTEGER CHECK (thinking_tokens IS NULL OR thinking_tokens >= 0),
    cached_tokens INTEGER CHECK (cached_tokens IS NULL OR cached_tokens >= 0),
    total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
    token_usage_status TEXT NOT NULL CHECK (
        token_usage_status IN ('VERIFIED_MEASURED', 'ESTIMATED', 'UNAVAILABLE_FROM_PROVIDER')
    ),
    actual_cost_usd REAL CHECK (actual_cost_usd IS NULL OR actual_cost_usd >= 0),
    estimated_cost_usd REAL CHECK (estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),
    cost_source TEXT NOT NULL CHECK (
        cost_source IN (
            'PROVIDER_BILLED',
            'ESTIMATED_LOCAL_PRICING_TABLE',
            'LOCAL_FREE_ZERO',
            'UNAVAILABLE_FROM_PROVIDER'
        )
    ),
    pricing_table_ref TEXT,
    telemetry_status TEXT NOT NULL CHECK (
        telemetry_status IN (
            'VERIFIED_MEASURED',
            'UNAVAILABLE_FROM_PROVIDER',
            'ESTIMATED_PENDING_AUDIT'
        )
    ),
    is_estimated INTEGER NOT NULL DEFAULT 0 CHECK (is_estimated IN (0, 1)),
    estimate_label TEXT,
    quota_units_used INTEGER CHECK (quota_units_used IS NULL OR quota_units_used >= 0),
    quota_limit_units INTEGER CHECK (quota_limit_units IS NULL OR quota_limit_units >= 0),
    quota_remaining_units INTEGER CHECK (quota_remaining_units IS NULL OR quota_remaining_units >= 0),
    idempotency_key TEXT CHECK (idempotency_key IS NULL OR length(trim(idempotency_key)) > 0),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    note TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    recorded_by TEXT NOT NULL CHECK (length(trim(recorded_by)) > 0),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    recorded_at TEXT NOT NULL CHECK (length(trim(recorded_at)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_ledger_idempotency
    ON resource_ledger_entries (task_id, COALESCE(run_id, ''), idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_resource_ledger_task_run
    ON resource_ledger_entries (task_id, run_id, recorded_at ASC);

CREATE INDEX IF NOT EXISTS idx_resource_ledger_provider_conn
    ON resource_ledger_entries (provider_id, connection_id, model_id, recorded_at ASC);

CREATE INDEX IF NOT EXISTS idx_resource_ledger_bucket
    ON resource_ledger_entries (quota_bucket_key, recorded_at ASC);

CREATE TRIGGER IF NOT EXISTS trg_resource_ledger_no_update
BEFORE UPDATE ON resource_ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'resource_ledger_entries is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_resource_ledger_no_delete
BEFORE DELETE ON resource_ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'resource_ledger_entries is append-only: DELETE is forbidden');
END;

CREATE TABLE IF NOT EXISTS resource_threshold_alerts (
    alert_id TEXT PRIMARY KEY CHECK (
        alert_id GLOB 'ralt_*' AND length(trim(alert_id)) >= 9
    ),
    dedup_key TEXT NOT NULL CHECK (length(trim(dedup_key)) > 0),
    policy_id TEXT NOT NULL REFERENCES resource_budget_policies(policy_id) ON DELETE RESTRICT,
    alert_category TEXT NOT NULL CHECK (
        alert_category IN ('BUDGET_COST', 'DURATION', 'MEMORY', 'TOKENS', 'QUOTA')
    ),
    scope_kind TEXT NOT NULL CHECK (
        scope_kind IN ('TASK', 'PROJECT', 'CONNECTION', 'QUOTA_BUCKET', 'PROVIDER_MODEL')
    ),
    scope_ref TEXT NOT NULL CHECK (length(trim(scope_ref)) > 0),
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    provider_id TEXT,
    connection_id TEXT,
    model_id TEXT,
    quota_bucket_key TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('WARNING', 'CRITICAL')),
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (
        status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'DISMISSED')
    ),
    observed_value REAL NOT NULL CHECK (observed_value >= 0),
    threshold_limit REAL NOT NULL CHECK (threshold_limit > 0),
    utilization_ratio REAL NOT NULL CHECK (utilization_ratio >= 0),
    is_estimated_basis INTEGER NOT NULL DEFAULT 0 CHECK (is_estimated_basis IN (0, 1)),
    estimate_label TEXT,
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    recommended_action TEXT NOT NULL CHECK (length(trim(recommended_action)) > 0),
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
    notification_id TEXT REFERENCES notifications(notification_id) ON DELETE RESTRICT,
    action_inbox_item_id TEXT REFERENCES action_inbox_items(inbox_item_id) ON DELETE RESTRICT,
    master_notified INTEGER NOT NULL DEFAULT 0 CHECK (master_notified IN (0, 1)),
    requires_operator_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_operator_approval IN (0, 1)),
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    acknowledgement_note TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution_note TEXT,
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_alerts_active_dedup
    ON resource_threshold_alerts (dedup_key)
    WHERE status IN ('OPEN', 'ACKNOWLEDGED');

CREATE INDEX IF NOT EXISTS idx_resource_alerts_scope_status
    ON resource_threshold_alerts (scope_kind, scope_ref, status);
