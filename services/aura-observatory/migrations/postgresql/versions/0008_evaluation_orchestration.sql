-- AURA-0092 durable Observatory evaluation orchestration state.
-- JSON columns hold only validated closed contract envelopes and bounded
-- references; provider locations and secrets never cross this boundary.

CREATE TABLE IF NOT EXISTS observatory_evaluation_jobs (
    job_id UUID PRIMARY KEY,
    request_id UUID NOT NULL UNIQUE,
    idempotency_key UUID NOT NULL UNIQUE,
    request_fingerprint CHAR(64) NOT NULL,
    request_version VARCHAR(64) NOT NULL,
    dataset_id UUID NOT NULL,
    dataset_version VARCHAR(64) NOT NULL,
    suite_id UUID NOT NULL,
    suite_version VARCHAR(64) NOT NULL,
    evaluator_id UUID NOT NULL,
    evaluator_version VARCHAR(64) NOT NULL,
    capture_classification VARCHAR(16) NOT NULL,
    deadline_at TIMESTAMPTZ NOT NULL,
    max_attempts SMALLINT NOT NULL,
    attempt_count SMALLINT NOT NULL DEFAULT 0,
    state VARCHAR(16) NOT NULL DEFAULT 'queued',
    envelope_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT observatory_evaluation_jobs_capture_ck CHECK (
        capture_classification IN ('metadata_only', 'hashed', 'redacted', 'sampled_content', 'full_content')
    ),
    CONSTRAINT observatory_evaluation_jobs_state_ck CHECK (
        state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'deadline')
    ),
    CONSTRAINT observatory_evaluation_jobs_attempts_ck CHECK (
        max_attempts BETWEEN 1 AND 10 AND attempt_count BETWEEN 0 AND 10
    ),
    CONSTRAINT observatory_evaluation_jobs_fingerprint_ck CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_jobs_due_idx
    ON observatory_evaluation_jobs(state, deadline_at, updated_at);

CREATE TABLE IF NOT EXISTS observatory_evaluation_attempts (
    attempt_id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES observatory_evaluation_jobs(job_id) ON DELETE CASCADE,
    attempt_number SMALLINT NOT NULL,
    request_id UUID NOT NULL,
    request_digest CHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'queued',
    next_attempt_at TIMESTAMPTZ NOT NULL,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    failure_code VARCHAR(32),
    terminal_reason VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (job_id, attempt_number),
    CONSTRAINT observatory_evaluation_attempts_state_ck CHECK (
        state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'deadline')
    ),
    CONSTRAINT observatory_evaluation_attempts_number_ck CHECK (attempt_number BETWEEN 1 AND 10),
    CONSTRAINT observatory_evaluation_attempts_digest_ck CHECK (
        request_digest ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_attempts_due_idx
    ON observatory_evaluation_attempts(state, next_attempt_at, lease_expires_at);

CREATE TABLE IF NOT EXISTS observatory_evaluation_schedules (
    schedule_id UUID PRIMARY KEY,
    occurrence_id UUID NOT NULL UNIQUE,
    dataset_id UUID NOT NULL,
    dataset_version VARCHAR(64) NOT NULL,
    suite_id UUID NOT NULL,
    suite_version VARCHAR(64) NOT NULL,
    evaluator_id UUID NOT NULL,
    evaluator_version VARCHAR(64) NOT NULL,
    request_envelope_json JSONB NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at TIMESTAMPTZ NOT NULL,
    cadence_seconds INTEGER NOT NULL,
    deadline_seconds INTEGER NOT NULL,
    max_attempts SMALLINT NOT NULL DEFAULT 3,
    state VARCHAR(16) NOT NULL DEFAULT 'ready',
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT observatory_evaluation_schedules_cadence_ck CHECK (
        cadence_seconds BETWEEN 60 AND 2592000
    ),
    CONSTRAINT observatory_evaluation_schedules_deadline_ck CHECK (
        deadline_seconds BETWEEN 1 AND 2592000
    ),
    CONSTRAINT observatory_evaluation_schedules_attempts_ck CHECK (max_attempts BETWEEN 1 AND 10),
    CONSTRAINT observatory_evaluation_schedules_state_ck CHECK (state IN ('ready', 'paused'))
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_schedules_due_idx
    ON observatory_evaluation_schedules(enabled, next_run_at, lease_expires_at, schedule_id);


CREATE TABLE IF NOT EXISTS observatory_evaluation_outbox (
    event_id UUID PRIMARY KEY,
    event_type VARCHAR(48) NOT NULL,
    job_id UUID NOT NULL REFERENCES observatory_evaluation_jobs(job_id) ON DELETE CASCADE,
    attempt_id UUID NOT NULL REFERENCES observatory_evaluation_attempts(attempt_id) ON DELETE CASCADE,
    envelope_json JSONB NOT NULL,
    published_at TIMESTAMPTZ,
    publish_attempts SMALLINT NOT NULL DEFAULT 0,
    last_error_code VARCHAR(32),
    next_publish_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT observatory_evaluation_outbox_type_ck CHECK (
        event_type IN ('evaluation.runner.requested', 'evaluation.runner.cancellation_requested')
    ),
    CONSTRAINT observatory_evaluation_outbox_attempts_ck CHECK (publish_attempts BETWEEN 0 AND 10)
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_outbox_pending_idx
    ON observatory_evaluation_outbox(published_at, next_publish_at, lease_expires_at, created_at, event_id);

CREATE TABLE IF NOT EXISTS observatory_evaluation_result_receipts (
    result_id UUID PRIMARY KEY,
    message_id UUID NOT NULL UNIQUE,
    job_id UUID NOT NULL REFERENCES observatory_evaluation_jobs(job_id) ON DELETE CASCADE,
    attempt_id UUID NOT NULL REFERENCES observatory_evaluation_attempts(attempt_id) ON DELETE CASCADE,
    request_digest CHAR(64) NOT NULL,
    result_digest CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    disposition VARCHAR(24) NOT NULL,
    envelope_json JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT observatory_evaluation_receipts_status_ck CHECK (
        status IN ('completed', 'cancelled', 'failed')
    ),
    CONSTRAINT observatory_evaluation_receipts_disposition_ck CHECK (
        disposition IN ('applied', 'duplicate', 'rejected', 'out_of_order')
    ),
    CONSTRAINT observatory_evaluation_receipts_request_digest_ck CHECK (
        request_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT observatory_evaluation_receipts_result_digest_ck CHECK (
        result_digest ~ '^[0-9a-f]{64}$'
    )
);

CREATE TABLE IF NOT EXISTS observatory_evaluation_lifecycle_audits (
    audit_id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES observatory_evaluation_jobs(job_id) ON DELETE CASCADE,
    attempt_id UUID REFERENCES observatory_evaluation_attempts(attempt_id) ON DELETE SET NULL,
    event_type VARCHAR(48) NOT NULL,
    outcome VARCHAR(24) NOT NULL,
    failure_code VARCHAR(32),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_lifecycle_audits_job_idx
    ON observatory_evaluation_lifecycle_audits(job_id, occurred_at, audit_id);

CREATE TABLE IF NOT EXISTS observatory_evaluation_worker_leases (
    component_id VARCHAR(96) PRIMARY KEY,
    lease_token UUID NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    backlog_count INTEGER NOT NULL DEFAULT 0,
    outbox_pending_count INTEGER NOT NULL DEFAULT 0,
    result_lag_count INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT observatory_evaluation_worker_leases_counts_ck CHECK (
        backlog_count >= 0 AND outbox_pending_count >= 0 AND result_lag_count >= 0
    )
);

-- Forward-adjust the fixed metric label columns before AURA-0092 writes use
-- the longer, still bounded orchestration operation names (for example
-- ``schedule_materialize``).  This preserves the existing primary key and
-- prevents valid allowlisted metrics from failing persistence on upgraded
-- Observatory schemas.
DO $$
BEGIN
    EXECUTE 'ALTER ' || 'TABLE observatory_evaluation_metric_series '
        || 'ALTER COLUMN label_operation TYPE VARCHAR(64), '
        || 'ALTER COLUMN label_outcome TYPE VARCHAR(64), '
        || 'ALTER COLUMN label_selector TYPE VARCHAR(64), '
        || 'ALTER COLUMN label_phase TYPE VARCHAR(64), '
        || 'ALTER COLUMN label_failure_code TYPE VARCHAR(64)';
END
$$;

-- Forward-adjust the shared, service-owned metric allowlist while preserving
-- every previously accepted AURA-0007 series. This is safe for fresh and
-- upgraded Observatory schemas and keeps metric names contract-stable.
DO $$
BEGIN
    EXECUTE 'ALTER ' || 'TABLE observatory_evaluation_metric_series '
        || 'DROP CONSTRAINT IF EXISTS observatory_evaluation_metric_name_ck';
    EXECUTE 'ALTER ' || 'TABLE observatory_evaluation_metric_series '
        || $ddl$ADD CONSTRAINT observatory_evaluation_metric_name_ck CHECK (metric_name IN (
        'observatory_evaluation_artifact_operation_count',
        'observatory_evaluation_artifact_operation_duration_ms',
        'observatory_evaluation_deletion_action_count',
        'observatory_evaluation_deletion_deleted_count',
        'observatory_evaluation_deletion_failure_count',
        'observatory_evaluation_deletion_duration_ms',
        'observatory_evaluation_job_admission_count',
        'observatory_evaluation_attempt_transition_count',
        'observatory_evaluation_retry_scheduled_count',
        'observatory_evaluation_terminal_count',
        'observatory_evaluation_dependency_outcome_count',
        'observatory_evaluation_cancellation_count',
        'observatory_evaluation_deadline_count',
        'observatory_evaluation_outbox_pending_count',
        'observatory_evaluation_backlog_count',
        'observatory_evaluation_outbox_oldest_age_seconds',
        'observatory_evaluation_outbox_publication_count',
        'observatory_evaluation_result_consumption_count',
        'observatory_evaluation_worker_heartbeat_age_seconds',
        'observatory_evaluation_scheduler_heartbeat_age_seconds',
        'observatory_evaluation_scheduler_occurrence_count',
        'observatory_evaluation_result_lag_seconds',
        'observatory_evaluation_operation_duration_ms'
    ))$ddl$;
END
$$;
