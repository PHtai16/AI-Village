-- AI Village Schema Migration 0009: Governed Agent Recruitment Proposals & Immutable Revisions
-- Persists candidate Agent recruitment proposals, evaluation outcomes, and immutable transition logs.

CREATE TABLE IF NOT EXISTS agent_recruitment_proposals (
    proposal_id TEXT PRIMARY KEY CHECK (length(trim(proposal_id)) > 0),
    candidate_agent_id TEXT NOT NULL CHECK (
        length(trim(candidate_agent_id)) > 0 AND candidate_agent_id != 'core.master'
    ),
    status TEXT NOT NULL CHECK (
        status IN ('PROPOSED', 'EVALUATED_PASS', 'EVALUATED_FAIL', 'ACTIVATED', 'REJECTED')
    ),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    proposed_by TEXT NOT NULL CHECK (length(trim(proposed_by)) > 0),
    proposal_json TEXT NOT NULL CHECK (json_valid(proposal_json)),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    evaluated_at TEXT,
    activated_at TEXT,
    rejected_at TEXT,
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_agent_recruitment_candidate
    ON agent_recruitment_proposals (candidate_agent_id, status);

CREATE INDEX IF NOT EXISTS idx_agent_recruitment_task
    ON agent_recruitment_proposals (task_id, status);

CREATE TABLE IF NOT EXISTS agent_recruitment_revisions (
    revision_id TEXT PRIMARY KEY CHECK (length(trim(revision_id)) > 0),
    proposal_id TEXT NOT NULL REFERENCES agent_recruitment_proposals(proposal_id) ON DELETE RESTRICT,
    candidate_agent_id TEXT NOT NULL CHECK (length(trim(candidate_agent_id)) > 0),
    record_version INTEGER NOT NULL CHECK (record_version >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('PROPOSED', 'EVALUATED_PASS', 'EVALUATED_FAIL', 'ACTIVATED', 'REJECTED')
    ),
    actor_id TEXT NOT NULL CHECK (length(trim(actor_id)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    timestamp TEXT NOT NULL CHECK (length(trim(timestamp)) > 0),
    UNIQUE (proposal_id, record_version)
);

CREATE INDEX IF NOT EXISTS idx_agent_recruitment_revisions_proposal
    ON agent_recruitment_revisions (proposal_id, record_version ASC);

CREATE TRIGGER IF NOT EXISTS trg_agent_recruitment_revisions_no_update
BEFORE UPDATE ON agent_recruitment_revisions
BEGIN
    SELECT RAISE(ABORT, 'agent_recruitment_revisions history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_agent_recruitment_revisions_no_delete
BEFORE DELETE ON agent_recruitment_revisions
BEGIN
    SELECT RAISE(ABORT, 'agent_recruitment_revisions history is immutable and cannot be deleted.');
END;
