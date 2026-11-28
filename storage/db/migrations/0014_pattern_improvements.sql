-- AI Village Schema Migration 0014: Persistent Multi-Task Pattern Detection & Improvements (P10.02)
-- Stores quantitative, deduplicated improvement suggestions backed by relational links to real tasks
-- and persisted P10.01 task retrospectives.

CREATE TABLE IF NOT EXISTS improvement_suggestions (
    improvement_id TEXT PRIMARY KEY CHECK (
        improvement_id GLOB 'imp_*' AND length(trim(improvement_id)) >= 8
    ),
    pattern_type TEXT NOT NULL CHECK (
        pattern_type IN ('repeated_error', 'redundant_operations', 'missing_skill_gap')
    ),
    pattern_key TEXT NOT NULL CHECK (length(trim(pattern_key)) > 0),
    suggested_skill_id TEXT NOT NULL CHECK (length(trim(suggested_skill_id)) > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    status TEXT NOT NULL CHECK (
        status IN ('SUGGESTED', 'CANDIDATE_CREATED', 'RESOLVED', 'DISMISSED')
    ),
    occurrence_count INTEGER NOT NULL CHECK (occurrence_count >= 2),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64),
    improvement_json TEXT NOT NULL CHECK (json_valid(improvement_json)),
    detected_by TEXT NOT NULL CHECK (length(trim(detected_by)) > 0),
    detected_at TEXT NOT NULL CHECK (length(trim(detected_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    UNIQUE (pattern_type, pattern_key)
);

CREATE INDEX IF NOT EXISTS idx_improvement_suggestions_status_type
    ON improvement_suggestions (status, pattern_type, updated_at DESC);

CREATE TABLE IF NOT EXISTS improvement_task_links (
    improvement_id TEXT NOT NULL REFERENCES improvement_suggestions(improvement_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    retrospective_id TEXT NOT NULL REFERENCES task_retrospectives(retrospective_id) ON DELETE RESTRICT,
    linked_at TEXT NOT NULL CHECK (length(trim(linked_at)) > 0),
    PRIMARY KEY (improvement_id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_improvement_task_links_task
    ON improvement_task_links (task_id, improvement_id);
