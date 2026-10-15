-- Observatory-owned transactional metadata.  Observations remain in
-- ClickHouse; this schema contains no automatic retention or TTL policy.
CREATE TABLE IF NOT EXISTS observatory_components (
    component_id VARCHAR(128) NOT NULL,
    version VARCHAR(64) NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (component_id, version)
);

CREATE TABLE IF NOT EXISTS observatory_deployments (
    deployment_id VARCHAR(128) NOT NULL,
    git_commit VARCHAR(64) NOT NULL,
    environment VARCHAR(32) NOT NULL,
    host_id VARCHAR(128) NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    service_roles VARCHAR[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (deployment_id, git_commit, environment, host_id)
);

CREATE TABLE IF NOT EXISTS observatory_ingest_counters (
    counter_key VARCHAR(32) PRIMARY KEY,
    accepted_count BIGINT NOT NULL DEFAULT 0,
    rejected_count BIGINT NOT NULL DEFAULT 0,
    duplicate_count BIGINT NOT NULL DEFAULT 0,
    malformed_count BIGINT NOT NULL DEFAULT 0,
    storage_failure_count BIGINT NOT NULL DEFAULT 0,
    duration_ms_total DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_ingest_at TIMESTAMPTZ,
    last_accepted_at TIMESTAMPTZ,
    last_failure_at TIMESTAMPTZ
);

CREATE SEQUENCE IF NOT EXISTS observatory_ingest_sequence AS BIGINT START WITH 1;

-- The atomic claim table gives duplicate delivery a durable outcome before the
-- ClickHouse insert. Failed or ambiguous inserts remain pending until their
-- deterministic lease expires, then recover with the same deduplication token.
CREATE TABLE IF NOT EXISTS observatory_observation_receipts (
    observation_id UUID PRIMARY KEY,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE observatory_observation_receipts
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'pending';
ALTER TABLE observatory_observation_receipts
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE TABLE IF NOT EXISTS observatory_service_configuration (
    configuration_key VARCHAR(128) PRIMARY KEY,
    configuration_value VARCHAR(512) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS observatory_retention_actions (
    action_id UUID PRIMARY KEY,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    requested_cutoff TIMESTAMPTZ,
    action_kind VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    deleted_count BIGINT NOT NULL DEFAULT 0,
    deleted_receipt_count BIGINT NOT NULL DEFAULT 0,
    deleted_run_count BIGINT NOT NULL DEFAULT 0
);

-- Query/retention extensions are idempotent and preserve catalogue,
-- configuration, session, and audit rows across deletion commands.
ALTER TABLE observatory_observation_receipts
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE observatory_retention_actions
    ADD COLUMN IF NOT EXISTS command_fingerprint VARCHAR(256),
    ADD COLUMN IF NOT EXISTS target_observation_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS target_receipt_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS target_run_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS deletion_phase VARCHAR(32) NOT NULL DEFAULT 'queued';
ALTER TABLE observatory_deployments
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN IF NOT EXISTS service_roles VARCHAR[] NOT NULL DEFAULT '{}';
ALTER TABLE observatory_retention_actions
    ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(200),
    ADD COLUMN IF NOT EXISTS confirmation VARCHAR(64),
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS error_code VARCHAR(64),
    ADD COLUMN IF NOT EXISTS deleted_receipt_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS deleted_run_count BIGINT NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS observatory_retention_actions_idempotency
    ON observatory_retention_actions(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS observatory_receipts_observed_at
    ON observatory_observation_receipts(observed_at);
CREATE INDEX IF NOT EXISTS observatory_retention_actions_status
    ON observatory_retention_actions(status, requested_at);
