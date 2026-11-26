-- AI Village Schema Migration 0006: Dynamic Team Proposals & Pinned Revisions
-- Stores capability-matched Worker team proposals, manifest revision pins, and immutable history.
-- Never creates execution Runs or fabricates runtime status.

CREATE TABLE IF NOT EXISTS team_proposals (
    proposal_id TEXT PRIMARY KEY CHECK (length(trim(proposal_id)) > 0),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    plan_id TEXT NOT NULL REFERENCES master_plans(plan_id) ON DELETE RESTRICT,
    plan_revision INTEGER NOT NULL CHECK (plan_revision >= 1),
    active_revision INTEGER NOT NULL CHECK (active_revision >= 1),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    status TEXT NOT NULL CHECK (
        status IN (
            'READY_FOR_ASSIGNMENT',
            'PARTIAL',
            'BLOCKED_MISSING_CAPABILITY',
            'BLOCKED_GOVERNANCE',
            'ASSIGNED',
            'STALE',
            'SUPERSEDED'
        )
    ),
    proposal_json TEXT NOT NULL CHECK (json_valid(proposal_json)),
    proposal_sha256 TEXT NOT NULL CHECK (length(proposal_sha256) = 64),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_by TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_team_proposals_task ON team_proposals (task_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_proposals_plan ON team_proposals (plan_id);
CREATE INDEX IF NOT EXISTS idx_team_proposals_status ON team_proposals (status);

CREATE TABLE IF NOT EXISTS team_proposal_revisions (
    proposal_id TEXT NOT NULL REFERENCES team_proposals(proposal_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    proposal_revision INTEGER NOT NULL CHECK (proposal_revision >= 1),
    status TEXT NOT NULL CHECK (
        status IN (
            'READY_FOR_ASSIGNMENT',
            'PARTIAL',
            'BLOCKED_MISSING_CAPABILITY',
            'BLOCKED_GOVERNANCE',
            'ASSIGNED',
            'STALE',
            'SUPERSEDED'
        )
    ),
    proposal_json TEXT NOT NULL CHECK (json_valid(proposal_json)),
    proposal_sha256 TEXT NOT NULL CHECK (length(proposal_sha256) = 64),
    change_summary TEXT NOT NULL CHECK (length(trim(change_summary)) > 0),
    changed_by TEXT NOT NULL CHECK (length(trim(changed_by)) > 0),
    changed_at TEXT NOT NULL CHECK (length(trim(changed_at)) > 0),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    PRIMARY KEY (proposal_id, proposal_revision)
);

CREATE INDEX IF NOT EXISTS idx_team_proposal_revisions_task ON team_proposal_revisions (task_id, proposal_revision);

CREATE TRIGGER IF NOT EXISTS trg_team_proposal_revisions_no_update
BEFORE UPDATE ON team_proposal_revisions
BEGIN
    SELECT RAISE(ABORT, 'team_proposal_revisions history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_team_proposal_revisions_no_delete
BEFORE DELETE ON team_proposal_revisions
BEGIN
    SELECT RAISE(ABORT, 'team_proposal_revisions history is immutable and cannot be deleted.');
END;
