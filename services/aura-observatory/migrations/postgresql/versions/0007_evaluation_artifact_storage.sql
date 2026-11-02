-- AURA-0090: Observatory-owned evaluation artifact metadata and deletion
-- ledger.  Raw bytes remain in the separately scoped Garage bucket;
-- this schema intentionally contains no provider locator, path, content,
-- principal identity, endpoint, or secret.
CREATE TABLE IF NOT EXISTS observatory_evaluation_authorizations (
    authorization_record_id UUID PRIMARY KEY,
    request_id UUID NOT NULL,
    dataset_id UUID NOT NULL,
    capture_classification VARCHAR(16) NOT NULL,
    authorization_purpose VARCHAR(32) NOT NULL,
    authorization_context VARCHAR(32) NOT NULL DEFAULT 'controlled_evaluation',
    retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    authorized_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT observatory_evaluation_authorizations_class_ck
        CHECK (capture_classification IN ('redacted', 'sampled_content', 'full_content')),
    CONSTRAINT observatory_evaluation_authorizations_retention_ck
        CHECK (retention_mode = 'manual_only'),
    CONSTRAINT observatory_evaluation_authorizations_context_ck
        CHECK (authorization_context = 'controlled_evaluation'),
    CONSTRAINT observatory_evaluation_authorizations_purpose_ck
        CHECK (authorization_purpose IN (
            'synthetic_benchmark', 'owner_supplied_evaluation',
            'controlled_replay', 'human_review'
        ))
);

CREATE TABLE IF NOT EXISTS observatory_evaluation_artifacts (
    artifact_id UUID NOT NULL,
    artifact_reference_id UUID PRIMARY KEY,
    request_id UUID NOT NULL,
    result_id UUID NOT NULL,
    idempotency_key UUID NOT NULL UNIQUE,
    authorization_record_id UUID NOT NULL REFERENCES observatory_evaluation_authorizations(authorization_record_id),
    capture_classification VARCHAR(16) NOT NULL,
    media_classification VARCHAR(32) NOT NULL,
    media_type VARCHAR(64) NOT NULL,
    byte_count BIGINT NOT NULL DEFAULT 0,
    sha256 CHAR(64) NOT NULL,
    job_id UUID NOT NULL,
    dataset_id UUID NOT NULL,
    suite_id UUID NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    failure_code VARCHAR(64),
    declared_max_bytes BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT observatory_evaluation_artifacts_status_ck
        CHECK (status IN ('pending', 'available', 'failed', 'deleting')),
    CONSTRAINT observatory_evaluation_artifacts_capture_ck
        CHECK (capture_classification IN ('redacted', 'sampled_content', 'full_content')),
    CONSTRAINT observatory_evaluation_artifacts_sha_ck
        CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT observatory_evaluation_artifacts_byte_ck
        CHECK (byte_count >= 0 AND byte_count <= 1073741824)
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_artifacts_job_idx
    ON observatory_evaluation_artifacts(job_id, created_at);
CREATE INDEX IF NOT EXISTS observatory_evaluation_artifacts_dataset_idx
    ON observatory_evaluation_artifacts(dataset_id, created_at);
CREATE INDEX IF NOT EXISTS observatory_evaluation_artifacts_suite_idx
    ON observatory_evaluation_artifacts(suite_id, created_at);
CREATE INDEX IF NOT EXISTS observatory_evaluation_artifacts_created_idx
    ON observatory_evaluation_artifacts(created_at, artifact_reference_id);

CREATE TABLE IF NOT EXISTS observatory_evaluation_deletion_commands (
    command_id UUID PRIMARY KEY,
    idempotency_key UUID NOT NULL UNIQUE,
    selector_kind VARCHAR(16) NOT NULL,
    selector_value VARCHAR(128),
    confirmation VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'queued',
    failure_code VARCHAR(64),
    deleted_count BIGINT NOT NULL DEFAULT 0,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    deletion_phase VARCHAR(16) NOT NULL DEFAULT 'queued',
    CONSTRAINT observatory_evaluation_deletion_selector_ck
        CHECK (selector_kind IN ('job', 'dataset', 'suite', 'cutoff', 'purge_all')),
    CONSTRAINT observatory_evaluation_deletion_status_ck
        CHECK (status IN ('queued', 'running', 'completed')),
    CONSTRAINT observatory_evaluation_deletion_confirmation_ck
        CHECK (confirmation = 'DELETE_EVALUATION_ARTIFACTS')
);

CREATE TABLE IF NOT EXISTS observatory_evaluation_deletion_targets (
    command_id UUID NOT NULL REFERENCES observatory_evaluation_deletion_commands(command_id) ON DELETE CASCADE,
    artifact_reference_id UUID NOT NULL,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (command_id, artifact_reference_id)
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_deletion_targets_pending_idx
    ON observatory_evaluation_deletion_targets(command_id, completed_at, artifact_reference_id);
-- A reference can belong to only one live deletion snapshot.  This is the
-- database-level ownership transition that prevents overlapping commands from
-- deleting/counting the same artifact concurrently.
CREATE UNIQUE INDEX IF NOT EXISTS observatory_evaluation_deletion_targets_live_ref_idx
    ON observatory_evaluation_deletion_targets(artifact_reference_id)
    WHERE completed_at IS NULL;

-- Score IDs are captured from ClickHouse before a command is queued.  This
-- ledger is independent from artifact targets so metadata-only scores and
-- scores without an artifact reference are deleted on retries as well.
CREATE TABLE IF NOT EXISTS observatory_evaluation_deletion_score_targets (
    command_id UUID NOT NULL REFERENCES observatory_evaluation_deletion_commands(command_id) ON DELETE CASCADE,
    score_id UUID NOT NULL,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (command_id, score_id)
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_deletion_score_targets_pending_idx
    ON observatory_evaluation_deletion_score_targets(command_id, completed_at, score_id);

CREATE TABLE IF NOT EXISTS observatory_evaluation_deletion_audits (
    audit_id UUID PRIMARY KEY,
    command_id UUID NOT NULL REFERENCES observatory_evaluation_deletion_commands(command_id),
    outcome VARCHAR(16) NOT NULL,
    failure_code VARCHAR(64),
    deleted_count BIGINT NOT NULL DEFAULT 0,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT observatory_evaluation_deletion_audits_outcome_ck
        CHECK (outcome IN ('completed', 'failed'))
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_deletion_audits_command_idx
    ON observatory_evaluation_deletion_audits(command_id, recorded_at);

CREATE UNIQUE INDEX IF NOT EXISTS observatory_evaluation_deletion_audits_id_idx
    ON observatory_evaluation_deletion_audits(audit_id)
    WHERE audit_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS observatory_evaluation_deletion_commands_lease_idx
    ON observatory_evaluation_deletion_commands(status, lease_expires_at, requested_at);

-- Worker/API processes share only this bounded, content-free metric series
-- table. Labels use fixed columns so arbitrary JSON attributes cannot enter
-- the operational metrics boundary.
CREATE TABLE IF NOT EXISTS observatory_evaluation_metric_series (
    metric_name VARCHAR(96) NOT NULL,
    label_operation VARCHAR(16) NOT NULL DEFAULT '',
    label_outcome VARCHAR(16) NOT NULL DEFAULT '',
    label_selector VARCHAR(16) NOT NULL DEFAULT '',
    label_phase VARCHAR(16) NOT NULL DEFAULT '',
    label_failure_code VARCHAR(32) NOT NULL DEFAULT '',
    value DOUBLE PRECISION NOT NULL DEFAULT 0,
    histogram_count BIGINT NOT NULL DEFAULT 0,
    histogram_buckets JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (
        metric_name, label_operation, label_outcome, label_selector,
        label_phase, label_failure_code
    ),
    CONSTRAINT observatory_evaluation_metric_name_ck CHECK (
        metric_name IN (
            'observatory_evaluation_artifact_operation_count',
            'observatory_evaluation_artifact_operation_duration_ms',
            'observatory_evaluation_deletion_action_count',
            'observatory_evaluation_deletion_deleted_count',
            'observatory_evaluation_deletion_failure_count',
            'observatory_evaluation_deletion_duration_ms'
        )
    ),
    CONSTRAINT observatory_evaluation_metric_value_ck CHECK (
        abs(value) <= 1000000000000000
    ),
    CONSTRAINT observatory_evaluation_metric_histogram_ck CHECK (
        histogram_count >= 0
    )
);
