-- AI Village Schema Migration 0012: Vault Artifacts, Product Preview, Evidence Records, Knowledge Search, Trash & Backups
-- Implements P08.01-P08.05 content-addressed blob storage, immutable version lineage, shared reference protection, and backups.

CREATE TABLE IF NOT EXISTS vault_blobs (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    storage_relpath TEXT NOT NULL UNIQUE CHECK (length(trim(storage_relpath)) > 0),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    content_type TEXT NOT NULL CHECK (length(trim(content_type)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    purged_at TEXT
);

CREATE TABLE IF NOT EXISTS artifact_registry_entries (
    artifact_id TEXT PRIMARY KEY CHECK (length(trim(artifact_id)) > 0),
    logical_key TEXT NOT NULL CHECK (length(trim(logical_key)) > 0),
    version TEXT NOT NULL CHECK (length(trim(version)) > 0),
    version_number INTEGER NOT NULL CHECK (version_number >= 1),
    previous_artifact_id TEXT REFERENCES artifact_registry_entries(artifact_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    owner_agent_id TEXT NOT NULL CHECK (length(trim(owner_agent_id)) > 0),
    relative_path TEXT NOT NULL CHECK (length(trim(relative_path)) > 0),
    blob_sha256 TEXT NOT NULL REFERENCES vault_blobs(sha256) ON DELETE RESTRICT,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    content_type TEXT NOT NULL CHECK (length(trim(content_type)) > 0),
    verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED' CHECK (
        verification_status IN ('UNVERIFIED', 'VERIFIED_PASS', 'VERIFIED_FAIL')
    ),
    evidence_reference TEXT,
    access_scope TEXT NOT NULL DEFAULT 'task' CHECK (
        access_scope IN ('private', 'task', 'project', 'global')
    ),
    department TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags_json)),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
        lifecycle_status IN ('ACTIVE', 'TRASHED', 'PURGED')
    ),
    trashed_at TEXT,
    trashed_by TEXT,
    trash_reason TEXT,
    purged_at TEXT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    UNIQUE (logical_key, version),
    UNIQUE (logical_key, version_number),
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT,
    CHECK (verification_status != 'VERIFIED_PASS' OR evidence_reference IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_vault_art_key_ver ON artifact_registry_entries (logical_key, version_number);
CREATE INDEX IF NOT EXISTS idx_vault_art_task_life ON artifact_registry_entries (task_id, lifecycle_status);
CREATE INDEX IF NOT EXISTS idx_vault_art_blob_life ON artifact_registry_entries (blob_sha256, lifecycle_status);

CREATE TABLE IF NOT EXISTS artifact_task_references (
    artifact_id TEXT NOT NULL REFERENCES artifact_registry_entries(artifact_id) ON DELETE RESTRICT,
    referencing_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    referencing_run_id TEXT,
    referenced_by TEXT NOT NULL CHECK (length(trim(referenced_by)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    PRIMARY KEY (artifact_id, referencing_task_id)
);

CREATE INDEX IF NOT EXISTS idx_vault_ref_task ON artifact_task_references (referencing_task_id, artifact_id);

CREATE TABLE IF NOT EXISTS evidence_records (
    evidence_id TEXT PRIMARY KEY CHECK (length(trim(evidence_id)) > 0),
    evidence_kind TEXT NOT NULL CHECK (
        evidence_kind IN (
            'qa_log',
            'evidence_link',
            'research_source',
            'execution_trace',
            'build_test_log',
            'screenshot_proof'
        )
    ),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT,
    artifact_id TEXT REFERENCES artifact_registry_entries(artifact_id) ON DELETE RESTRICT,
    recorded_by TEXT NOT NULL CHECK (length(trim(recorded_by)) > 0),
    source_url TEXT,
    retrieved_at TEXT,
    untrusted_source INTEGER NOT NULL DEFAULT 0 CHECK (untrusted_source IN (0, 1)),
    content_sha256 TEXT CHECK (content_sha256 IS NULL OR length(content_sha256) = 64),
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    redacted_payload_json TEXT NOT NULL CHECK (json_valid(redacted_payload_json)),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    CHECK (run_id IS NULL OR task_id IS NOT NULL),
    FOREIGN KEY (run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_evidence_task_run_kind ON evidence_records (task_id, run_id, evidence_kind);

CREATE TRIGGER IF NOT EXISTS trg_evidence_records_no_update
BEFORE UPDATE ON evidence_records
BEGIN
    SELECT RAISE(ABORT, 'evidence_records history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_evidence_records_no_delete
BEFORE DELETE ON evidence_records
BEGIN
    SELECT RAISE(ABORT, 'evidence_records history is immutable and cannot be deleted.');
END;

CREATE TABLE IF NOT EXISTS knowledge_entries (
    knowledge_id TEXT PRIMARY KEY CHECK (length(trim(knowledge_id)) > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    category TEXT NOT NULL CHECK (
        category IN (
            'architecture',
            'standard',
            'playbook',
            'research_finding',
            'postmortem',
            'deliverable_summary'
        )
    ),
    version TEXT NOT NULL CHECK (length(trim(version)) > 0),
    access_scope TEXT NOT NULL CHECK (access_scope IN ('private', 'task', 'project', 'global')),
    owner_agent_id TEXT NOT NULL CHECK (length(trim(owner_agent_id)) > 0),
    department TEXT,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    project_id TEXT,
    source_artifact_id TEXT REFERENCES artifact_registry_entries(artifact_id) ON DELETE RESTRICT,
    source_evidence_id TEXT REFERENCES evidence_records(evidence_id) ON DELETE RESTRICT,
    tags_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags_json)),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    content_markdown TEXT NOT NULL CHECK (length(trim(content_markdown)) > 0),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    approval_status TEXT NOT NULL DEFAULT 'APPROVED' CHECK (
        approval_status IN ('DRAFT', 'APPROVED', 'ARCHIVED')
    ),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_scope_cat ON knowledge_entries (access_scope, department, category, approval_status);

CREATE TABLE IF NOT EXISTS vault_backups (
    backup_id TEXT PRIMARY KEY CHECK (length(trim(backup_id)) > 0),
    backup_relpath TEXT NOT NULL CHECK (length(trim(backup_relpath)) > 0),
    db_sha256 TEXT NOT NULL CHECK (length(db_sha256) = 64),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    blob_count INTEGER NOT NULL CHECK (blob_count >= 0),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    retention_days INTEGER NOT NULL DEFAULT 30 CHECK (retention_days >= 1)
);
