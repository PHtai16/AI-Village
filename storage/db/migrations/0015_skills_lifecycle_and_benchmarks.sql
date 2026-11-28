-- AI Village Schema Migration 0015: Persistent Skills Lifecycle, Benchmarks, Proposals, Outcomes & Rollback (P10.03-P10.05)

CREATE TABLE IF NOT EXISTS skill_packages (
    package_id TEXT PRIMARY KEY CHECK (
        package_id GLOB 'skp_*' AND length(trim(package_id)) >= 8
    ),
    skill_id TEXT NOT NULL CHECK (length(trim(skill_id)) >= 2),
    version TEXT NOT NULL CHECK (length(trim(version)) >= 5),
    lifecycle_status TEXT NOT NULL CHECK (
        lifecycle_status IN (
            'DRAFT', 'CANDIDATE', 'UNDER_REVIEW',
            'APPROVED', 'REJECTED', 'DEPRECATED', 'DISABLED'
        )
    ),
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low', 'medium', 'high')),
    has_scripts INTEGER NOT NULL DEFAULT 0 CHECK (has_scripts IN (0, 1)),
    requires_human_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_human_approval IN (0, 1)),
    author TEXT NOT NULL CHECK (length(trim(author)) > 0),
    maintainer TEXT NOT NULL CHECK (maintainer IN ('it', 'process', 'qa')),
    category TEXT NOT NULL CHECK (length(trim(category)) > 0),
    source_improvement_id TEXT REFERENCES improvement_suggestions(improvement_id) ON DELETE RESTRICT,
    storage_zone TEXT NOT NULL CHECK (storage_zone IN ('candidates', 'approved')),
    package_relpath TEXT NOT NULL CHECK (length(trim(package_relpath)) > 0),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    package_sha256 TEXT NOT NULL CHECK (length(package_sha256) = 64),
    record_json TEXT NOT NULL CHECK (json_valid(record_json)),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_by TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    UNIQUE (skill_id, version)
);

CREATE INDEX IF NOT EXISTS idx_skill_packages_status
    ON skill_packages (skill_id, lifecycle_status, updated_at DESC);

CREATE TABLE IF NOT EXISTS skill_active_pointers (
    skill_id TEXT PRIMARY KEY CHECK (length(trim(skill_id)) >= 2),
    active_version TEXT,
    previous_approved_version TEXT,
    updated_by TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    last_audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (skill_id, active_version)
        REFERENCES skill_packages(skill_id, version) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS skill_it_reviews (
    review_id TEXT PRIMARY KEY CHECK (
        review_id GLOB 'itr_*' AND length(trim(review_id)) >= 8
    ),
    skill_id TEXT NOT NULL,
    version TEXT NOT NULL,
    package_sha256 TEXT NOT NULL CHECK (length(package_sha256) = 64),
    reviewer_id TEXT NOT NULL CHECK (length(trim(reviewer_id)) > 0),
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    review_json TEXT NOT NULL CHECK (json_valid(review_json)),
    reviewed_at TEXT NOT NULL CHECK (length(trim(reviewed_at)) > 0),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (skill_id, version)
        REFERENCES skill_packages(skill_id, version) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_skill_it_reviews_pkg
    ON skill_it_reviews (skill_id, version, reviewed_at DESC);

CREATE TABLE IF NOT EXISTS skill_benchmarks (
    benchmark_id TEXT PRIMARY KEY CHECK (
        benchmark_id GLOB 'bmk_*' AND length(trim(benchmark_id)) >= 8
    ),
    skill_id TEXT NOT NULL,
    version TEXT NOT NULL,
    package_sha256 TEXT NOT NULL CHECK (length(package_sha256) = 64),
    benchmark_status TEXT NOT NULL CHECK (
        benchmark_status IN (
            'NOT_BENCHMARKED', 'IN_PROGRESS', 'PASSED', 'FAILED', 'INCONCLUSIVE'
        )
    ),
    qa_actor_id TEXT NOT NULL CHECK (length(trim(qa_actor_id)) > 0),
    evidence_id TEXT REFERENCES evidence_records(evidence_id) ON DELETE RESTRICT,
    benchmark_json TEXT NOT NULL CHECK (json_valid(benchmark_json)),
    recorded_at TEXT NOT NULL CHECK (length(trim(recorded_at)) > 0),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (skill_id, version)
        REFERENCES skill_packages(skill_id, version) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_skill_benchmarks_pkg
    ON skill_benchmarks (skill_id, version, recorded_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_skill_benchmarks_no_update
BEFORE UPDATE ON skill_benchmarks
BEGIN
    SELECT RAISE(ABORT, 'skill_benchmarks history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_skill_benchmarks_no_delete
BEFORE DELETE ON skill_benchmarks
BEGIN
    SELECT RAISE(ABORT, 'skill_benchmarks history is immutable and cannot be deleted.');
END;

CREATE TABLE IF NOT EXISTS skill_activation_proposals (
    proposal_id TEXT PRIMARY KEY CHECK (
        proposal_id GLOB 'sap_*' AND length(trim(proposal_id)) >= 8
    ),
    skill_id TEXT NOT NULL,
    version TEXT NOT NULL,
    package_sha256 TEXT NOT NULL CHECK (length(package_sha256) = 64),
    proposal_status TEXT NOT NULL CHECK (
        proposal_status IN ('PENDING_APPROVAL', 'PENDING_ACTIVATION', 'ACTIVATED', 'REJECTED')
    ),
    requires_human_approval INTEGER NOT NULL CHECK (requires_human_approval IN (0, 1)),
    approval_id TEXT REFERENCES approval_requests(approval_id) ON DELETE RESTRICT,
    proposed_by TEXT NOT NULL CHECK (length(trim(proposed_by)) > 0),
    proposal_json TEXT NOT NULL CHECK (json_valid(proposal_json)),
    proposed_at TEXT NOT NULL CHECK (length(trim(proposed_at)) > 0),
    decided_by TEXT,
    decided_at TEXT,
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (skill_id, version)
        REFERENCES skill_packages(skill_id, version) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_skill_activation_proposals_status
    ON skill_activation_proposals (proposal_status, skill_id, version);

CREATE TABLE IF NOT EXISTS skill_improvement_outcomes (
    outcome_id TEXT PRIMARY KEY CHECK (
        outcome_id GLOB 'sko_*' AND length(trim(outcome_id)) >= 8
    ),
    skill_id TEXT NOT NULL,
    version TEXT NOT NULL,
    source_improvement_id TEXT REFERENCES improvement_suggestions(improvement_id) ON DELETE RESTRICT,
    regression_detected INTEGER NOT NULL CHECK (regression_detected IN (0, 1)),
    outcome_json TEXT NOT NULL CHECK (json_valid(outcome_json)),
    recorded_at TEXT NOT NULL CHECK (length(trim(recorded_at)) > 0),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (skill_id, version)
        REFERENCES skill_packages(skill_id, version) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_skill_outcomes_pkg
    ON skill_improvement_outcomes (skill_id, version, recorded_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_skill_improvement_outcomes_no_update
BEFORE UPDATE ON skill_improvement_outcomes
BEGIN
    SELECT RAISE(ABORT, 'skill_improvement_outcomes history is immutable and cannot be updated.');
END;

CREATE TRIGGER IF NOT EXISTS trg_skill_improvement_outcomes_no_delete
BEFORE DELETE ON skill_improvement_outcomes
BEGIN
    SELECT RAISE(ABORT, 'skill_improvement_outcomes history is immutable and cannot be deleted.');
END;
