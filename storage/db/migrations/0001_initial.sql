-- AI Village Initial SQLite Schema (Version 1)
-- Aligns strictly with Core Contracts (P04.01-P04.03) without creating a parallel Runtime.

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT,
    description TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS connections (
    provider_id TEXT NOT NULL CHECK (length(trim(provider_id)) > 0),
    connection_id TEXT NOT NULL CHECK (length(trim(connection_id)) > 0),
    model_id TEXT,
    project_ref TEXT,
    credential_ref TEXT CHECK (
        credential_ref IS NULL OR (
            length(trim(credential_ref)) > 0
            AND instr(credential_ref, '=') = 0
            AND instr(credential_ref, ' ') = 0
            AND substr(lower(credential_ref), 1, 4) NOT IN ('aiza', 'ghp_', 'gho_')
            AND instr(lower(credential_ref), 'sk-') != 1
            AND instr(lower(credential_ref), 'github_pat_') != 1
            AND instr(lower(credential_ref), 'bearer ') != 1
            AND instr(lower(credential_ref), '-----begin') != 1
        )
    ),
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (provider_id, connection_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(project_id) ON DELETE RESTRICT,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    requested_by TEXT NOT NULL CHECK (length(trim(requested_by)) > 0),
    status TEXT NOT NULL CHECK (
        status IN (
            'QUEUED',
            'ASSIGNED',
            'RUNNING',
            'WAITING_INPUT',
            'WAITING_APPROVAL',
            'BLOCKED',
            'COMPLETED',
            'FAILED',
            'CANCELLED'
        )
    ),
    priority TEXT NOT NULL CHECK (priority IN ('LOW', 'NORMAL', 'HIGH', 'URGENT')),
    created_at TEXT,
    updated_at TEXT,
    assigned_agent_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(assigned_agent_ids_json)),
    checklist_item_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(checklist_item_ids_json)),
    active_run_id TEXT,
    artifact_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(artifact_ids_json)),
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(acceptance_criteria_json)),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    baseline_json TEXT NOT NULL CHECK (json_valid(baseline_json)),
    CHECK (updated_at IS NULL OR created_at IS NOT NULL),
    CHECK (status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED') OR active_run_id IS NULL),
    FOREIGN KEY (active_run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    runtime_id TEXT NOT NULL CHECK (length(trim(runtime_id)) > 0),
    invocation_id TEXT,
    correlation_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'PENDING',
            'RUNNING',
            'WAITING_INPUT',
            'WAITING_APPROVAL',
            'BLOCKED',
            'SUCCEEDED',
            'FAILED',
            'CANCELLED',
            'TIMED_OUT'
        )
    ),
    started_at TEXT,
    finished_at TEXT,
    session_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(session_ids_json)),
    provider_id TEXT,
    connection_id TEXT,
    model_id TEXT,
    artifact_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(artifact_ids_json)),
    error_reference_json TEXT CHECK (
        error_reference_json IS NULL OR json_valid(error_reference_json)
    ),
    telemetry_total_duration_ms INTEGER CHECK (
        telemetry_total_duration_ms IS NULL OR telemetry_total_duration_ms >= 0
    ),
    telemetry_provider_duration_ms INTEGER CHECK (
        telemetry_provider_duration_ms IS NULL OR telemetry_provider_duration_ms >= 0
    ),
    telemetry_engine_call_count INTEGER CHECK (
        telemetry_engine_call_count IS NULL OR telemetry_engine_call_count >= 0
    ),
    telemetry_input_tokens INTEGER CHECK (
        telemetry_input_tokens IS NULL OR telemetry_input_tokens >= 0
    ),
    telemetry_output_tokens INTEGER CHECK (
        telemetry_output_tokens IS NULL OR telemetry_output_tokens >= 0
    ),
    telemetry_thinking_tokens INTEGER CHECK (
        telemetry_thinking_tokens IS NULL OR telemetry_thinking_tokens >= 0
    ),
    telemetry_cached_tokens INTEGER CHECK (
        telemetry_cached_tokens IS NULL OR telemetry_cached_tokens >= 0
    ),
    telemetry_total_tokens INTEGER CHECK (
        telemetry_total_tokens IS NULL OR telemetry_total_tokens >= 0
    ),
    telemetry_estimated_cost_usd REAL CHECK (
        telemetry_estimated_cost_usd IS NULL OR telemetry_estimated_cost_usd >= 0
    ),
    telemetry_actual_cost_usd REAL CHECK (
        telemetry_actual_cost_usd IS NULL OR telemetry_actual_cost_usd >= 0
    ),
    telemetry_status TEXT CHECK (
        telemetry_status IS NULL OR telemetry_status IN (
            'VERIFIED_MEASURED',
            'UNAVAILABLE_FROM_PROVIDER',
            'ESTIMATED_PENDING_AUDIT'
        )
    ),
    telemetry_source TEXT CHECK (
        telemetry_source IS NULL OR telemetry_source IN (
            'provider_cli',
            'provider_api',
            'adapter_measured',
            'unavailable'
        )
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    baseline_json TEXT NOT NULL CHECK (json_valid(baseline_json)),
    UNIQUE (run_id, task_id),
    CHECK (status != 'PENDING' OR (started_at IS NULL AND finished_at IS NULL)),
    CHECK (finished_at IS NULL OR started_at IS NOT NULL),
    CHECK (status != 'SUCCEEDED' OR error_reference_json IS NULL),
    CHECK (
        (telemetry_status IS NULL AND telemetry_source IS NULL)
        OR (telemetry_status IS NOT NULL AND telemetry_source IS NOT NULL)
    ),
    CHECK (
        telemetry_status IS NULL
        OR NOT (telemetry_status = 'VERIFIED_MEASURED' AND telemetry_source = 'unavailable')
    )
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    legacy_session_ref TEXT,
    agent_id TEXT NOT NULL CHECK (length(trim(agent_id)) > 0),
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    invocation_id TEXT,
    provider_id TEXT,
    connection_id TEXT,
    provider_conversation_id TEXT,
    workspace_reference TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('INITIALIZED', 'ACTIVE', 'IDLE', 'CLOSED', 'TERMINATED', 'FAILED')
    ),
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT,
    CHECK (
        provider_conversation_id IS NULL
        OR lower(provider_conversation_id) != lower(session_id)
    ),
    CHECK (invocation_id IS NULL OR invocation_id != session_id),
    CHECK (status != 'INITIALIZED' OR (started_at IS NULL AND finished_at IS NULL)),
    CHECK (finished_at IS NULL OR started_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS checklist_items (
    checklist_item_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT,
    assigned_agent_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'PENDING',
            'IN_PROGRESS',
            'WAITING_APPROVAL',
            'BLOCKED',
            'COMPLETED',
            'FAILED',
            'SKIPPED',
            'CANCELLED'
        )
    ),
    order_index INTEGER NOT NULL CHECK (order_index >= 0 AND order_index <= 10000),
    evidence_reference TEXT,
    created_at TEXT,
    completed_at TEXT,
    CHECK (
        status != 'COMPLETED'
        OR (evidence_reference IS NOT NULL AND completed_at IS NOT NULL)
    ),
    CHECK (status = 'COMPLETED' OR completed_at IS NULL)
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    owner_agent_id TEXT NOT NULL CHECK (length(trim(owner_agent_id)) > 0),
    relative_path TEXT NOT NULL CHECK (length(trim(relative_path)) > 0),
    storage_ref TEXT,
    content_type TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    version TEXT,
    verification_status TEXT NOT NULL CHECK (
        verification_status IN ('UNVERIFIED', 'VERIFIED_PASS', 'VERIFIED_FAIL')
    ),
    evidence_reference TEXT,
    created_at TEXT,
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT,
    CHECK (
        verification_status != 'VERIFIED_PASS'
        OR (evidence_reference IS NOT NULL AND sha256 IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS events (
    sequence_number INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL CHECK (length(trim(timestamp)) > 0),
    event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
    source TEXT NOT NULL CHECK (
        source IN (
            'core_runtime',
            'orchestrator',
            'agent_runtime',
            'engine_adapter',
            'provider',
            'tool_runtime',
            'qa_verifier',
            'governance',
            'operator'
        )
    ),
    task_id TEXT,
    run_id TEXT,
    session_id TEXT,
    actor_id TEXT,
    provider_id TEXT,
    connection_id TEXT,
    model_id TEXT,
    artifact_id TEXT,
    evidence_id TEXT,
    correlation_id TEXT,
    redacted_payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(redacted_payload_json)),
    batch_id TEXT,
    CHECK (
        task_id IS NOT NULL
        OR run_id IS NOT NULL
        OR session_id IS NOT NULL
        OR actor_id IS NOT NULL
        OR artifact_id IS NOT NULL
        OR correlation_id IS NOT NULL
    )
);

CREATE TRIGGER IF NOT EXISTS trg_events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: DELETE is forbidden');
END;

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    notification_type TEXT NOT NULL CHECK (
        notification_type IN (
            'TASK_UPDATE',
            'RUN_FAILED',
            'APPROVAL_REQUIRED',
            'QA_RESULT',
            'RESOURCE_WARNING',
            'SECURITY_ALERT',
            'SYSTEM_NOTICE'
        )
    ),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks (status, priority);
CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks (project_id);
CREATE INDEX IF NOT EXISTS idx_runs_task_id_status ON runs (task_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_correlation_id ON runs (correlation_id);
CREATE INDEX IF NOT EXISTS idx_sessions_task_id ON sessions (task_id);
CREATE INDEX IF NOT EXISTS idx_sessions_run_id ON sessions (run_id);
CREATE INDEX IF NOT EXISTS idx_sessions_provider_conv ON sessions (provider_conversation_id);
CREATE INDEX IF NOT EXISTS idx_checklist_task_order ON checklist_items (task_id, order_index);
CREATE INDEX IF NOT EXISTS idx_artifacts_task_run ON artifacts (task_id, run_id);
CREATE INDEX IF NOT EXISTS idx_events_task_seq ON events (task_id, sequence_number);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events (run_id, sequence_number);
CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events (session_id, sequence_number);
CREATE INDEX IF NOT EXISTS idx_events_corr_seq ON events (correlation_id, sequence_number);
CREATE INDEX IF NOT EXISTS idx_events_type_seq ON events (event_type, sequence_number);
CREATE INDEX IF NOT EXISTS idx_notifications_task_created ON notifications (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_event_id ON notifications (event_id);
