-- High-cardinality observations are owned by Observatory ClickHouse.
-- observation_id is the unique content-free deduplication identity.
-- Deliberately no TTL clause: retention is an explicit authorized action.
CREATE TABLE IF NOT EXISTS observatory_migration_ledger (
    version String,
    applied_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(applied_at)
ORDER BY version;

CREATE TABLE IF NOT EXISTS observations (
    observation_id String,
    ingest_sequence UInt64,
    observed_at DateTime64(3, 'UTC'),
    schema_version UInt8,
    kind LowCardinality(String),
    component_id LowCardinality(String),
    component_version LowCardinality(String),
    operation LowCardinality(String),
    measurement_name LowCardinality(String),
    measurement_value Float64,
    measurement_unit LowCardinality(String),
    dimensions Map(String, String),
    trace_id String,
    span_id String,
    parent_span_id String,
    linked_span_ids Array(String),
    run_id String,
    conversation_id String,
    deployment_id String,
    git_commit String,
    environment LowCardinality(String),
    host_id String,
    service_role LowCardinality(String),
    capture_policy LowCardinality(String)
) ENGINE = ReplacingMergeTree
ORDER BY (observation_id, observed_at, component_id, operation)
SETTINGS non_replicated_deduplication_window = 100000;

ALTER TABLE observations ADD COLUMN IF NOT EXISTS ingest_sequence UInt64 DEFAULT 0;

-- Content-free derived run projection. The materialized view maintains it as
-- observations arrive; retention explicitly removes affected rows because
-- ClickHouse mutations do not cascade to derived tables.
-- Existing schemas receive a deterministic backfill in migration 0002.
CREATE TABLE IF NOT EXISTS run_projections (
    run_id String,
    last_seen_at AggregateFunction(max, DateTime64(3, 'UTC')),
    observation_count AggregateFunction(sum, UInt64)
) ENGINE = AggregatingMergeTree
ORDER BY run_id;

CREATE MATERIALIZED VIEW IF NOT EXISTS run_projections_mv
TO run_projections AS
SELECT
    run_id,
    maxState(observed_at) AS last_seen_at,
    sumState(toUInt64(1)) AS observation_count
FROM observations
WHERE run_id != ''
GROUP BY run_id;
