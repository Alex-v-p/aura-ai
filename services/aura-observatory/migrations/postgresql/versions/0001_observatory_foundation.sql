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
    last_ingest_at TIMESTAMPTZ
);

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
    completed_at TIMESTAMPTZ,
    deleted_count BIGINT NOT NULL DEFAULT 0
);
