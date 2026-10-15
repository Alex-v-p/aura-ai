-- High-cardinality observations are owned by Observatory ClickHouse.
-- Deliberately no TTL clause: retention is an explicit authorized action.
CREATE TABLE IF NOT EXISTS observations (
    observation_id String,
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
