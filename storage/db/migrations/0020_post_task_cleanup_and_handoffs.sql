-- AI Village Schema Migration 0020: Safe Post-Task Resource Cleanup, Inventory & Process Handoff (P11.05)

CREATE TABLE IF NOT EXISTS post_task_cleanups (
    cleanup_id TEXT PRIMARY KEY CHECK (
        cleanup_id GLOB 'clnp_*' AND length(trim(cleanup_id)) >= 9
    ),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    task_status_snapshot TEXT NOT NULL CHECK (length(trim(task_status_snapshot)) > 0),
    cleanup_status TEXT NOT NULL CHECK (
        cleanup_status IN ('COMPLETED', 'PARTIAL', 'BLOCKED', 'SKIPPED')
    ),
    idempotency_key TEXT CHECK (idempotency_key IS NULL OR length(trim(idempotency_key)) > 0),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    inventory_json TEXT NOT NULL CHECK (json_valid(inventory_json)),
    preserved_refs_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(preserved_refs_json)),
    handoff_id TEXT,
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    performed_by TEXT NOT NULL CHECK (length(trim(performed_by)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_post_task_cleanup_idem
    ON post_task_cleanups (task_id, COALESCE(run_id, ''), idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_post_task_cleanups_task
    ON post_task_cleanups (task_id, created_at ASC);

CREATE TABLE IF NOT EXISTS post_task_cleanup_items (
    item_id TEXT PRIMARY KEY CHECK (length(trim(item_id)) > 0),
    cleanup_id TEXT NOT NULL REFERENCES post_task_cleanups(cleanup_id) ON DELETE RESTRICT,
    resource_kind TEXT NOT NULL CHECK (
        resource_kind IN (
            'TEMP_FILE',
            'TEMP_DIR',
            'EXECUTION_LEASE',
            'WORKER_SESSION',
            'ARTIFACT_VAULT',
            'EVIDENCE_RECORD',
            'LEDGER_ENTRY',
            'WORKSPACE_FILE'
        )
    ),
    target_ref TEXT NOT NULL CHECK (length(trim(target_ref)) > 0),
    action TEXT NOT NULL CHECK (
        action IN (
            'DELETED',
            'RELEASED',
            'CLOSED',
            'PRESERVED',
            'BLOCKED_UNSAFE_PATH',
            'BLOCKED_SHARED_REF',
            'FAILED'
        )
    ),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_cleanup_items_parent
    ON post_task_cleanup_items (cleanup_id, resource_kind, action);

CREATE TRIGGER IF NOT EXISTS trg_cleanup_items_no_update
BEFORE UPDATE ON post_task_cleanup_items
BEGIN
    SELECT RAISE(ABORT, 'post_task_cleanup_items is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_cleanup_items_no_delete
BEFORE DELETE ON post_task_cleanup_items
BEGIN
    SELECT RAISE(ABORT, 'post_task_cleanup_items is append-only: DELETE is forbidden');
END;

CREATE TABLE IF NOT EXISTS process_learning_handoffs (
    handoff_id TEXT PRIMARY KEY CHECK (
        handoff_id GLOB 'phnd_*' AND length(trim(handoff_id)) >= 9
    ),
    cleanup_id TEXT NOT NULL REFERENCES post_task_cleanups(cleanup_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    retrospective_id TEXT REFERENCES task_retrospectives(retrospective_id) ON DELETE RESTRICT,
    resource_summary_json TEXT NOT NULL CHECK (json_valid(resource_summary_json)),
    cleanup_inventory_json TEXT NOT NULL CHECK (json_valid(cleanup_inventory_json)),
    alerts_summary_json TEXT NOT NULL CHECK (json_valid(alerts_summary_json)),
    handoff_status TEXT NOT NULL CHECK (
        handoff_status IN ('DELIVERED_TO_PROCESS', 'PENDING_PROCESS_LINK', 'ALREADY_HANDED_OFF')
    ),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_process_handoffs_unique_task_run
    ON process_learning_handoffs (task_id, COALESCE(run_id, ''));

CREATE TRIGGER IF NOT EXISTS trg_process_handoffs_no_update
BEFORE UPDATE ON process_learning_handoffs
BEGIN
    SELECT RAISE(ABORT, 'process_learning_handoffs is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_process_handoffs_no_delete
BEFORE DELETE ON process_learning_handoffs
BEGIN
    SELECT RAISE(ABORT, 'process_learning_handoffs is append-only: DELETE is forbidden');
END;
