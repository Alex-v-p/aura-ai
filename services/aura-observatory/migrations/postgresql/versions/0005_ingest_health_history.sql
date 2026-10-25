-- Bounded, content-free Observatory self-monitoring state and history.
-- This migration is forward-only and rerunnable; health rows are operational
-- metadata and are never part of observation retention deletion.
ALTER TABLE observatory_ingest_counters
    ADD COLUMN IF NOT EXISTS rejection_reasons JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS storage_latency JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS storage_latency_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS storage_latency_sum DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS storage_latency_buckets JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS query_latency JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS last_observed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS worker_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS visibility_watermark BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS visibility_lag_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS backlog_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS oldest_backlog_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS query_freshness_lag_ms DOUBLE PRECISION;
ALTER TABLE observatory_ingest_counters
    ADD COLUMN IF NOT EXISTS last_rejection_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_duplicate_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_storage_failure_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS observatory_ingest_health_history (
    snapshot_id UUID PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    state VARCHAR(16) NOT NULL,
    intake_lag_ms DOUBLE PRECISION,
    visibility_watermark BIGINT NOT NULL DEFAULT 0,
    visibility_lag_count BIGINT NOT NULL DEFAULT 0,
    rejection_reasons JSONB NOT NULL DEFAULT '{}'::jsonb,
    duplicate_count BIGINT NOT NULL DEFAULT 0,
    storage_latency JSONB NOT NULL DEFAULT '{}'::jsonb,
    query_latency JSONB NOT NULL DEFAULT '{}'::jsonb,
    query_freshness_lag_ms DOUBLE PRECISION,
    worker_heartbeat_at TIMESTAMPTZ,
    worker_heartbeat_age_ms DOUBLE PRECISION,
    backlog_count BIGINT NOT NULL DEFAULT 0,
    oldest_backlog_age_ms DOUBLE PRECISION,
    event_sequence BIGINT,
    CONSTRAINT observatory_health_state CHECK (
        state IN ('healthy', 'delayed', 'partial', 'degraded', 'unavailable')
    )
);

CREATE INDEX IF NOT EXISTS observatory_health_history_observed
    ON observatory_ingest_health_history(observed_at DESC, snapshot_id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS observatory_health_history_event_sequence
    ON observatory_ingest_health_history(event_sequence)
    WHERE event_sequence IS NOT NULL;

-- A bounded history is retained independently of observation deletion. The
-- service prunes after each insert, so this remains deterministic without a
-- background TTL or scheduler dependency.
