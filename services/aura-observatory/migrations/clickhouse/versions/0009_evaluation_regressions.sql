-- AURA-0096 bounded high-cardinality comparison facts.  The API projection is
-- authoritative in PostgreSQL; this table contains only bounded metadata.
CREATE TABLE IF NOT EXISTS evaluation_regression_metric_facts (
    projection_id UUID,
    regression_id UUID,
    regression_version LowCardinality(String),
    source_result_id UUID,
    exact_version LowCardinality(String),
    metric_id LowCardinality(String),
    threshold_index UInt16 DEFAULT 0,
    metric_class LowCardinality(String),
    cohort_id LowCardinality(String),
    side LowCardinality(String),
    value Float64,
    sample_count UInt32,
    observed_at DateTime64(3, 'UTC'),
    evidence_state LowCardinality(String) DEFAULT 'complete',
    late UInt8 DEFAULT 0,
    capture_policy LowCardinality(String) DEFAULT 'metadata_only'
) ENGINE = ReplacingMergeTree(observed_at)
ORDER BY (projection_id, source_result_id, metric_id, cohort_id, side)
SETTINGS index_granularity = 8192;
ALTER TABLE evaluation_regression_metric_facts
    ADD COLUMN IF NOT EXISTS projection_id UUID;
INSERT INTO observatory_migration_ledger(version, applied_at)
SELECT '0009_evaluation_regressions', now64(3)
WHERE NOT EXISTS (SELECT 1 FROM observatory_migration_ledger WHERE version = '0009_evaluation_regressions');
