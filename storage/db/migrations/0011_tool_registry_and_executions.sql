-- AI Village Schema Migration 0011: Tool Registry and Immutable Tool Execution Records
-- Persists P07.01-P07.06 independent tool manifests and secret-redacted execution audit logs.

CREATE TABLE IF NOT EXISTS tool_registry (
    tool_id TEXT PRIMARY KEY CHECK (tool_id GLOB 'tools.*.*'),
    version TEXT NOT NULL CHECK (length(trim(version)) > 0),
    category TEXT NOT NULL CHECK (category IN ('files', 'code', 'browser', 'research', 'office')),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'DEGRADED', 'DISABLED')),
    execution_mode TEXT NOT NULL CHECK (execution_mode IN ('sandbox_only', 'restricted_local', 'approved_external')),
    risk_level TEXT NOT NULL CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH')),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    registered_at TEXT NOT NULL CHECK (length(trim(registered_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_tool_registry_cat_status ON tool_registry (category, status);

CREATE TABLE IF NOT EXISTS tool_executions (
    execution_id TEXT PRIMARY KEY CHECK (length(trim(execution_id)) > 0),
    tool_id TEXT NOT NULL CHECK (length(trim(tool_id)) > 0),
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT,
    session_id TEXT,
    actor_id TEXT NOT NULL CHECK (length(trim(actor_id)) > 0),
    action TEXT NOT NULL CHECK (length(trim(action)) > 0),
    status TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED', 'DENIED', 'APPROVAL_REQUIRED', 'TIMEOUT', 'CANCELLED')),
    business_verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED' CHECK (business_verification_status = 'UNVERIFIED'),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    estimated_cost_usd REAL,
    approval_id TEXT,
    audit_event_id TEXT,
    redacted_input_json TEXT NOT NULL CHECK (json_valid(redacted_input_json)),
    redacted_output_json TEXT NOT NULL CHECK (json_valid(redacted_output_json)),
    artifact_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(artifact_ids_json)),
    evidence_refs_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence_refs_json)),
    error_code TEXT,
    error_message TEXT,
    executed_at TEXT NOT NULL CHECK (length(trim(executed_at)) > 0),
    CHECK (run_id IS NULL OR task_id IS NOT NULL),
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_tool_executions_task_run ON tool_executions (task_id, run_id, executed_at);
CREATE INDEX IF NOT EXISTS idx_tool_executions_actor ON tool_executions (actor_id, executed_at);
CREATE INDEX IF NOT EXISTS idx_tool_executions_tool_status ON tool_executions (tool_id, status);

CREATE TRIGGER IF NOT EXISTS trg_tool_executions_no_update
BEFORE UPDATE ON tool_executions
BEGIN
    SELECT RAISE(ABORT, 'tool_executions history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_tool_executions_no_delete
BEFORE DELETE ON tool_executions
BEGIN
    SELECT RAISE(ABORT, 'tool_executions history is immutable and cannot be deleted.');
END;
