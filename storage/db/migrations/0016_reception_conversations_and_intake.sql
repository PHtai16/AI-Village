-- AI Village Schema Migration 0016: Durable Reception Intake, Conversation Isolation, and Task Refinements (P11.01)

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY CHECK (
        conversation_id GLOB 'conv_*' AND length(trim(conversation_id)) >= 9
    ),
    project_id TEXT REFERENCES projects(project_id) ON DELETE RESTRICT,
    owner_actor_id TEXT NOT NULL CHECK (length(trim(owner_actor_id)) > 0),
    title TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
        status IN ('ACTIVE', 'ARCHIVED', 'CLOSED')
    ),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_conversations_owner_status
    ON conversations (owner_actor_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS intake_requests (
    intake_id TEXT PRIMARY KEY CHECK (
        intake_id GLOB 'intk_*' AND length(trim(intake_id)) >= 9
    ),
    conversation_id TEXT REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
    idempotency_scope TEXT NOT NULL DEFAULT 'user_intake' CHECK (length(trim(idempotency_scope)) > 0),
    idempotency_key TEXT CHECK (idempotency_key IS NULL OR length(trim(idempotency_key)) > 0),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    source_channel TEXT NOT NULL CHECK (
        source_channel IN ('user', 'runtime', 'tool_runtime', 'telemetry', 'governance')
    ),
    submitted_by_actor_id TEXT NOT NULL CHECK (length(trim(submitted_by_actor_id)) > 0),
    submitted_by_actor_kind TEXT NOT NULL CHECK (
        submitted_by_actor_kind IN (
            'human_operator', 'master_agent', 'worker_agent', 'runtime_service', 'trusted_verifier'
        )
    ),
    intent_mode TEXT NOT NULL CHECK (
        intent_mode IN ('AUTO', 'NEW_TASK', 'REFINEMENT', 'AMBIGUOUS', 'SIGNAL')
    ),
    classification TEXT NOT NULL CHECK (
        classification IN (
            'QUEUED', 'APPENDED', 'CLARIFICATION_REQUIRED',
            'ANOMALOUS', 'EMERGENCY', 'APPROVAL_REQUIRED'
        )
    ),
    decision TEXT NOT NULL CHECK (
        decision IN (
            'CREATED_INDEPENDENT_TASK',
            'QUEUED_FOR_MASTER_TRIAGE',
            'APPENDED_REFINEMENT',
            'HELD_FOR_CLARIFICATION',
            'ESCALATED_EMERGENCY',
            'ROUTED_ANOMALY',
            'AWAITING_APPROVAL'
        )
    ),
    processing_status TEXT NOT NULL CHECK (
        processing_status IN (
            'RECEIVED',
            'QUEUED_FOR_MASTER',
            'APPLIED_TO_TASK',
            'NEEDS_CLARIFICATION',
            'MASTER_ACKNOWLEDGED',
            'RESOLVED',
            'REJECTED'
        )
    ),
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    related_task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    created_task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    observed_running_task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    approval_id TEXT,
    route_targets_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(route_targets_json)),
    escalated_to_master INTEGER NOT NULL DEFAULT 0 CHECK (escalated_to_master IN (0, 1)),
    master_handoff_status TEXT NOT NULL CHECK (
        master_handoff_status IN (
            'PENDING_MASTER_REVIEW',
            'NOT_APPLICABLE',
            'ACKNOWLEDGED_BY_MASTER',
            'PLANNED_BY_MASTER',
            'DISMISSED_BY_MASTER'
        )
    ),
    master_decision_note TEXT,
    master_decided_by TEXT,
    master_decided_at TEXT,
    correlation_id TEXT NOT NULL CHECK (length(trim(correlation_id)) > 0),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    outcome_json TEXT NOT NULL CHECK (json_valid(outcome_json)),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_idempotency
    ON intake_requests (idempotency_scope, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_intake_requests_conversation
    ON intake_requests (conversation_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_intake_requests_master_queue
    ON intake_requests (master_handoff_status, processing_status, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_intake_requests_tasks
    ON intake_requests (related_task_id, created_task_id, observed_running_task_id);

CREATE TABLE IF NOT EXISTS conversation_task_links (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE RESTRICT,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
    link_role TEXT NOT NULL CHECK (
        link_role IN ('PRIMARY', 'CONCURRENT_INDEPENDENT', 'REFINEMENT')
    ),
    linked_by_intake_id TEXT REFERENCES intake_requests(intake_id) ON DELETE RESTRICT,
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    linked_at TEXT NOT NULL CHECK (length(trim(linked_at)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_conversation_task_links_conv
    ON conversation_task_links (conversation_id, linked_at ASC);

CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id TEXT PRIMARY KEY CHECK (
        turn_id GLOB 'turn_*' AND length(trim(turn_id)) >= 9
    ),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
    turn_index INTEGER NOT NULL CHECK (turn_index >= 1),
    intake_id TEXT REFERENCES intake_requests(intake_id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    sender_actor_id TEXT NOT NULL CHECK (length(trim(sender_actor_id)) > 0),
    sender_kind TEXT NOT NULL CHECK (
        sender_kind IN (
            'human_operator', 'master_agent', 'worker_agent', 'runtime_service', 'trusted_verifier'
        )
    ),
    role TEXT NOT NULL CHECK (role IN ('user', 'reception', 'master', 'system')),
    message_text TEXT NOT NULL CHECK (length(trim(message_text)) > 0),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    UNIQUE (conversation_id, turn_index)
);

CREATE INDEX IF NOT EXISTS idx_conversation_turns_conv_idx
    ON conversation_turns (conversation_id, turn_index ASC);

CREATE TRIGGER IF NOT EXISTS trg_conversation_turns_no_update
BEFORE UPDATE ON conversation_turns
BEGIN
    SELECT RAISE(ABORT, 'conversation_turns is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_conversation_turns_no_delete
BEFORE DELETE ON conversation_turns
BEGIN
    SELECT RAISE(ABORT, 'conversation_turns is append-only: DELETE is forbidden');
END;

CREATE TABLE IF NOT EXISTS task_refinements (
    refinement_id TEXT PRIMARY KEY CHECK (
        refinement_id GLOB 'ref_*' AND length(trim(refinement_id)) >= 8
    ),
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    conversation_id TEXT REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
    intake_id TEXT NOT NULL UNIQUE REFERENCES intake_requests(intake_id) ON DELETE RESTRICT,
    sequence_in_task INTEGER NOT NULL CHECK (sequence_in_task >= 1),
    source_channel TEXT NOT NULL CHECK (
        source_channel IN ('user', 'runtime', 'tool_runtime', 'telemetry', 'governance')
    ),
    submitted_by TEXT NOT NULL CHECK (length(trim(submitted_by)) > 0),
    refinement_text TEXT NOT NULL CHECK (length(trim(refinement_text)) > 0),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    UNIQUE (task_id, sequence_in_task)
);

CREATE INDEX IF NOT EXISTS idx_task_refinements_task_seq
    ON task_refinements (task_id, sequence_in_task ASC);

CREATE TRIGGER IF NOT EXISTS trg_task_refinements_no_update
BEFORE UPDATE ON task_refinements
BEGIN
    SELECT RAISE(ABORT, 'task_refinements is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_refinements_no_delete
BEFORE DELETE ON task_refinements
BEGIN
    SELECT RAISE(ABORT, 'task_refinements is append-only: DELETE is forbidden');
END;

CREATE TABLE IF NOT EXISTS action_inbox_items (
    inbox_item_id TEXT PRIMARY KEY CHECK (length(trim(inbox_item_id)) > 0),
    intake_id TEXT REFERENCES intake_requests(intake_id) ON DELETE RESTRICT,
    conversation_id TEXT REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
    item_kind TEXT NOT NULL CHECK (
        item_kind IN (
            'APPROVAL_NEEDED',
            'EMERGENCY_ESCALATION',
            'ANOMALY_TRIAGE',
            'NEW_TASK_INTAKE',
            'TASK_REFINEMENT',
            'CLARIFICATION_NEEDED'
        )
    ),
    priority TEXT NOT NULL CHECK (priority IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (
        status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'DISMISSED')
    ),
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    approval_id TEXT,
    assigned_route_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(assigned_route_json)),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_action_inbox_items_status_prio
    ON action_inbox_items (status, priority, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_action_inbox_items_task
    ON action_inbox_items (task_id, created_at ASC);

CREATE TABLE IF NOT EXISTS reception_dedup_state (
    dedup_scope_key TEXT PRIMARY KEY CHECK (length(trim(dedup_scope_key)) > 0),
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    notification_type TEXT NOT NULL CHECK (length(trim(notification_type)) > 0),
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    dedup_token TEXT NOT NULL CHECK (length(trim(dedup_token)) > 0),
    occurrence_count INTEGER NOT NULL CHECK (occurrence_count >= 1),
    first_notification_id TEXT REFERENCES notifications(notification_id) ON DELETE RESTRICT,
    last_signal_id TEXT NOT NULL CHECK (length(trim(last_signal_id)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);
