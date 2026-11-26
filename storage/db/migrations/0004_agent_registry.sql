-- AI Village Schema Migration 0004: Durable Versioned Agent Registry
-- Stores declarative static Agent Registry state and immutable manifest revision history.
-- Intentionally omits dynamic runtime state (no is_running, tokens, or session counts).

CREATE TABLE IF NOT EXISTS agent_registry (
    agent_id TEXT PRIMARY KEY CHECK (length(trim(agent_id)) > 0),
    system_role TEXT NOT NULL CHECK (system_role IN ('orchestrator', 'worker')),
    department TEXT NOT NULL CHECK (length(trim(department)) > 0),
    registration_status TEXT NOT NULL CHECK (
        registration_status IN ('DRAFT', 'VALIDATED', 'ACTIVE', 'DISABLED', 'REJECTED')
    ),
    active_revision INTEGER NOT NULL CHECK (active_revision >= 1),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    spec_version TEXT NOT NULL CHECK (length(trim(spec_version)) > 0),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    capability_verification_json TEXT NOT NULL CHECK (json_valid(capability_verification_json)),
    source_path TEXT,
    registered_by TEXT NOT NULL CHECK (length(trim(registered_by)) > 0),
    registered_at TEXT NOT NULL CHECK (length(trim(registered_at)) > 0),
    updated_by TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    disabled_at TEXT,
    disabled_reason TEXT,
    last_audit_event_id TEXT REFERENCES events(event_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_agent_registry_status ON agent_registry (registration_status);
CREATE INDEX IF NOT EXISTS idx_agent_registry_dept ON agent_registry (department);

CREATE TABLE IF NOT EXISTS agent_manifest_versions (
    agent_id TEXT NOT NULL REFERENCES agent_registry(agent_id) ON DELETE RESTRICT,
    manifest_revision INTEGER NOT NULL CHECK (manifest_revision >= 1),
    spec_version TEXT NOT NULL CHECK (length(trim(spec_version)) > 0),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    change_summary TEXT NOT NULL CHECK (length(trim(change_summary)) > 0),
    changed_by TEXT NOT NULL CHECK (length(trim(changed_by)) > 0),
    changed_at TEXT NOT NULL CHECK (length(trim(changed_at)) > 0),
    audit_event_id TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    PRIMARY KEY (agent_id, manifest_revision)
);

CREATE INDEX IF NOT EXISTS idx_agent_versions_agent ON agent_manifest_versions (agent_id, manifest_revision);

CREATE TRIGGER IF NOT EXISTS trg_agent_manifest_versions_no_update
BEFORE UPDATE ON agent_manifest_versions
BEGIN
    SELECT RAISE(ABORT, 'agent_manifest_versions history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_agent_manifest_versions_no_delete
BEFORE DELETE ON agent_manifest_versions
BEGIN
    SELECT RAISE(ABORT, 'agent_manifest_versions history is immutable and cannot be deleted.');
END;
