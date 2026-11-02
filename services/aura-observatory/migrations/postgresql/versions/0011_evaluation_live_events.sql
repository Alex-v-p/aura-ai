-- AURA-0095 durable metadata-only evaluation live-event ledger.
--
-- This migration is deliberately forward-only and rerunnable.  The ledger
-- contains no payload, score, digest, authorization, provider, or subject
-- fields.  ``sequence`` is the durable Last-Event-ID ordering key; event_id
-- remains the public opaque UUID used by SSE clients.
CREATE TABLE IF NOT EXISTS observatory_evaluation_live_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    resource_type VARCHAR(32) NOT NULL,
    resource_id VARCHAR(128) NOT NULL,
    change VARCHAR(32) NOT NULL,
    state VARCHAR(64) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    capture_policy VARCHAR(16) NOT NULL DEFAULT 'metadata_only',
    CONSTRAINT observatory_eval_live_resource_type_ck CHECK (
        resource_type IN (
            'evaluation_dataset', 'evaluation_case', 'evaluation_suite',
            'evaluation_evaluator', 'evaluation_job', 'evaluation_attempt',
            'evaluation_result', 'evaluation_artifact', 'evaluation_review',
            'evaluation_calibration', 'evaluation_qualification',
            'evaluation_deletion'
        )
    ),
    CONSTRAINT observatory_eval_live_change_ck CHECK (
        change IN (
            'created', 'updated', 'state_changed', 'cancel_requested',
            'qualified', 'revoked', 'deletion_requested', 'deleted'
        )
    ),
    CONSTRAINT observatory_eval_live_capture_ck CHECK (capture_policy = 'metadata_only')
);

CREATE INDEX IF NOT EXISTS observatory_eval_live_sequence_idx
    ON observatory_evaluation_live_events(sequence);
CREATE INDEX IF NOT EXISTS observatory_eval_live_occurred_idx
    ON observatory_evaluation_live_events(occurred_at, sequence);

-- This ledger stores bounded content-free notifications. Ordinary notification
-- rows are deletable projections and are removed by matching owner-authorized
-- job/dataset/suite/cutoff/purge commands. The evaluation_deletion rows are
-- preserved content-free deletion-audit evidence. Neither class has an
-- automatic TTL; only explicit owner retention commands may remove rows.
COMMENT ON TABLE observatory_evaluation_live_events IS
    'Content-free evaluation notifications plus preserved deletion-audit evidence; ordinary notifications are manually deletable, no automatic TTL.';

-- Durable owner metadata needed by observatory-v1 projections.  These are
-- additive columns so 0011 remains safe for both fresh and upgraded schemas.
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_evaluation_attempts
    ADD COLUMN IF NOT EXISTS worker_component_id VARCHAR(96);
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_evaluation_deletion_targets
    ADD COLUMN IF NOT EXISTS artifact_id UUID,
    ADD COLUMN IF NOT EXISTS job_id UUID,
    ADD COLUMN IF NOT EXISTS dataset_id UUID,
    ADD COLUMN IF NOT EXISTS suite_id UUID;
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_reviews
    ADD COLUMN IF NOT EXISTS review_provenance_component VARCHAR(96),
    ADD COLUMN IF NOT EXISTS review_provenance_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS review_provenance_digest CHAR(64);

-- Revocation replay evidence is kept separately so the pre-existing
-- qualification table remains forward-compatible with older deployments.
create table if not exists observatory_evaluation_qualification_revocations (
    qualification_id UUID PRIMARY KEY
        REFERENCES observatory_local_judge_qualifications(qualification_id)
        ON DELETE CASCADE,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    request_fingerprint CHAR(64) NOT NULL,
    authorization_record_id UUID NOT NULL,
    revoked_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT observatory_eval_revoke_fingerprint_ck CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
    )
);
COMMENT ON TABLE observatory_evaluation_qualification_revocations IS
    'Content-free qualification revocation replay evidence; manual retention only.';

-- Completion is a terminal transition. One command may therefore have at
-- most one successful completion audit, even when workers retry after a
-- commit or race during recovery.
CREATE UNIQUE INDEX IF NOT EXISTS observatory_evaluation_deletion_audits_completed_idx
    ON observatory_evaluation_deletion_audits(command_id)
    WHERE outcome = 'completed';

-- Cancellation commands are metadata-only durable idempotency evidence.  They
-- bind one caller key and request fingerprint to one job without retaining
-- request data or secrets.
create table if not exists observatory_evaluation_cancellation_commands (
    idempotency_key VARCHAR(128) PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES observatory_evaluation_jobs(job_id) ON DELETE CASCADE,
    request_fingerprint CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (job_id),
    CONSTRAINT observatory_eval_cancel_fingerprint_ck CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
    )
);
COMMENT ON TABLE observatory_evaluation_cancellation_commands IS
    'Content-free cancellation idempotency evidence; manual retention only.';

-- Definition command replay is separate from immutable resource identity:
-- operation, canonical request fingerprint, and the closed metadata replay
-- survive API restart without retaining request content or provider details.
CREATE TABLE IF NOT EXISTS observatory_evaluation_definition_commands (
    idempotency_key VARCHAR(128) PRIMARY KEY,
    operation VARCHAR(32) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    resource_id UUID NOT NULL,
    resource_version VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    replay_json JSONB,
    claim_token VARCHAR(64),
    claimed_at TIMESTAMPTZ,
    claim_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT observatory_eval_definition_command_operation_ck CHECK (
        operation IN ('create_dataset', 'create_suite')
    ),
    CONSTRAINT observatory_eval_definition_command_status_ck CHECK (
        status IN ('pending', 'completed')
    ),
    CONSTRAINT observatory_eval_definition_command_fingerprint_ck CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX IF NOT EXISTS observatory_eval_definition_commands_resource_idx
    ON observatory_evaluation_definition_commands(operation, resource_id, resource_version);
ALTER TABLE observatory_evaluation_definition_commands
    ADD COLUMN IF NOT EXISTS claim_token VARCHAR(64),
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ;
COMMENT ON TABLE observatory_evaluation_definition_commands IS
    'Content-free dataset/suite command idempotency and closed replay metadata; fenced 120-second claims recover after worker interruption; manual retention only.';
