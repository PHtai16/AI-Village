-- AI Village Schema Migration 0017: Persistent Interrupt Classification, Routing, Dedup, ACK & Escalations (P11.02)

CREATE TABLE IF NOT EXISTS interrupt_incidents (
    incident_id TEXT PRIMARY KEY CHECK (
        incident_id GLOB 'inc_*' AND length(trim(incident_id)) >= 8
    ),
    interrupt_id TEXT NOT NULL UNIQUE CHECK (
        interrupt_id GLOB 'intr_*' AND length(trim(interrupt_id)) >= 9
    ),
    dedup_scope TEXT NOT NULL DEFAULT 'interrupt' CHECK (length(trim(dedup_scope)) > 0),
    incident_key TEXT CHECK (incident_key IS NULL OR length(trim(incident_key)) > 0),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    classification TEXT NOT NULL CHECK (
        classification IN (
            'INFORMATIONAL',
            'ATTENTION_NEEDED',
            'ACTION_REQUIRED',
            'HUMAN_INTERVENTION_REQUIRED',
            'CRITICAL_EMERGENCY'
        )
    ),
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    priority TEXT NOT NULL CHECK (
        priority IN ('P4_LOW', 'P3_NORMAL', 'P2_HIGH', 'P1_URGENT', 'P0_IMMEDIATE')
    ),
    urgency TEXT NOT NULL CHECK (urgency IN ('LOW', 'MODERATE', 'HIGH', 'IMMEDIATE')),
    impact TEXT NOT NULL CHECK (
        impact IN ('SINGLE_STEP', 'SINGLE_TASK', 'MULTI_TASK', 'WORKSPACE_WIDE', 'SYSTEM_WIDE')
    ),
    status TEXT NOT NULL CHECK (
        status IN (
            'CREATED',
            'PENDING_DELIVERY',
            'DELIVERED',
            'ACKNOWLEDGED',
            'ESCALATED',
            'RESOLVED',
            'DISMISSED'
        )
    ),
    source TEXT NOT NULL CHECK (
        source IN (
            'user',
            'runtime',
            'tool_runtime',
            'telemetry',
            'governance',
            'qa_verifier',
            'scheduler',
            'resources',
            'worker_report'
        )
    ),
    event_category TEXT NOT NULL CHECK (
        event_category IN (
            'status_update',
            'clarification',
            'anomaly',
            'resource_warning',
            'quota_exceeded',
            'security_breach',
            'approval_required',
            'run_failure',
            'emergency_stop',
            'specialist_question'
        )
    ),
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    details TEXT,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    conversation_id TEXT REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
    approval_id TEXT,
    routing_targets_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(routing_targets_json)),
    broadcast_all_departments INTEGER NOT NULL DEFAULT 0 CHECK (broadcast_all_departments = 0),
    master_notified INTEGER NOT NULL DEFAULT 0 CHECK (master_notified IN (0, 1)),
    action_inbox_item_id TEXT REFERENCES action_inbox_items(inbox_item_id) ON DELETE RESTRICT,
    notification_id TEXT REFERENCES notifications(notification_id) ON DELETE RESTRICT,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
    policy_rationale TEXT NOT NULL CHECK (length(trim(policy_rationale)) > 0),
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    acknowledgement_note TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution_outcome TEXT,
    resolution_note TEXT,
    escalation_count INTEGER NOT NULL DEFAULT 0 CHECK (escalation_count >= 0),
    submitted_by_actor_id TEXT NOT NULL CHECK (length(trim(submitted_by_actor_id)) > 0),
    submitted_by_actor_kind TEXT NOT NULL CHECK (
        submitted_by_actor_kind IN (
            'human_operator', 'master_agent', 'worker_agent', 'runtime_service', 'trusted_verifier'
        )
    ),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_interrupt_active_incident
    ON interrupt_incidents (dedup_scope, incident_key)
    WHERE incident_key IS NOT NULL AND status NOT IN ('RESOLVED', 'DISMISSED');

CREATE INDEX IF NOT EXISTS idx_interrupt_incidents_status_prio
    ON interrupt_incidents (status, severity, priority, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_interrupt_incidents_task
    ON interrupt_incidents (task_id, run_id, created_at ASC);

CREATE TABLE IF NOT EXISTS interrupt_events (
    interrupt_event_id TEXT PRIMARY KEY CHECK (
        interrupt_event_id GLOB 'intr_*' AND length(trim(interrupt_event_id)) >= 9
    ),
    incident_id TEXT NOT NULL REFERENCES interrupt_incidents(incident_id) ON DELETE RESTRICT,
    dedup_scope TEXT NOT NULL DEFAULT 'interrupt' CHECK (length(trim(dedup_scope)) > 0),
    event_dedup_key TEXT CHECK (event_dedup_key IS NULL OR length(trim(event_dedup_key)) > 0),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    source TEXT NOT NULL CHECK (
        source IN (
            'user',
            'runtime',
            'tool_runtime',
            'telemetry',
            'governance',
            'qa_verifier',
            'scheduler',
            'resources',
            'worker_report'
        )
    ),
    event_category TEXT NOT NULL CHECK (
        event_category IN (
            'status_update',
            'clarification',
            'anomaly',
            'resource_warning',
            'quota_exceeded',
            'security_breach',
            'approval_required',
            'run_failure',
            'emergency_stop',
            'specialist_question'
        )
    ),
    dedup_reason TEXT NOT NULL CHECK (
        dedup_reason IN (
            'NEW_INCIDENT',
            'EXACT_EVENT_RETRY',
            'ACTIVE_INCIDENT_AGGREGATED',
            'POST_RESOLUTION_RECURRENCE'
        )
    ),
    deduplicated INTEGER NOT NULL CHECK (deduplicated IN (0, 1)),
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    notification_suppressed INTEGER NOT NULL CHECK (notification_suppressed IN (0, 1)),
    notification_id TEXT REFERENCES notifications(notification_id) ON DELETE RESTRICT,
    submitted_by_actor_id TEXT NOT NULL CHECK (length(trim(submitted_by_actor_id)) > 0),
    outcome_json TEXT NOT NULL CHECK (json_valid(outcome_json)),
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_interrupt_events_dedup
    ON interrupt_events (dedup_scope, event_dedup_key)
    WHERE event_dedup_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_interrupt_events_incident
    ON interrupt_events (incident_id, created_at ASC);

CREATE TRIGGER IF NOT EXISTS trg_interrupt_events_no_update
BEFORE UPDATE ON interrupt_events
BEGIN
    SELECT RAISE(ABORT, 'interrupt_events is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_interrupt_events_no_delete
BEFORE DELETE ON interrupt_events
BEGIN
    SELECT RAISE(ABORT, 'interrupt_events is append-only: DELETE is forbidden');
END;

CREATE TABLE IF NOT EXISTS interrupt_escalations (
    escalation_id TEXT PRIMARY KEY CHECK (
        escalation_id GLOB 'esc_*' AND length(trim(escalation_id)) >= 8
    ),
    incident_id TEXT NOT NULL REFERENCES interrupt_incidents(incident_id) ON DELETE RESTRICT,
    from_classification TEXT NOT NULL CHECK (
        from_classification IN (
            'INFORMATIONAL',
            'ATTENTION_NEEDED',
            'ACTION_REQUIRED',
            'HUMAN_INTERVENTION_REQUIRED',
            'CRITICAL_EMERGENCY'
        )
    ),
    to_classification TEXT NOT NULL CHECK (
        to_classification IN (
            'INFORMATIONAL',
            'ATTENTION_NEEDED',
            'ACTION_REQUIRED',
            'HUMAN_INTERVENTION_REQUIRED',
            'CRITICAL_EMERGENCY'
        )
    ),
    from_severity TEXT NOT NULL CHECK (from_severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    to_severity TEXT NOT NULL CHECK (to_severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    escalated_by TEXT NOT NULL CHECK (length(trim(escalated_by)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    added_targets_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(added_targets_json)),
    notification_id TEXT REFERENCES notifications(notification_id) ON DELETE RESTRICT,
    audit_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_interrupt_escalations_incident
    ON interrupt_escalations (incident_id, created_at ASC);

CREATE TRIGGER IF NOT EXISTS trg_interrupt_escalations_no_update
BEFORE UPDATE ON interrupt_escalations
BEGIN
    SELECT RAISE(ABORT, 'interrupt_escalations is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_interrupt_escalations_no_delete
BEFORE DELETE ON interrupt_escalations
BEGIN
    SELECT RAISE(ABORT, 'interrupt_escalations is append-only: DELETE is forbidden');
END;
