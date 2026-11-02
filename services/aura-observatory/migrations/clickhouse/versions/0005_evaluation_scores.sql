-- AURA-0090 finite evaluation scores.  No raw content, maps, arbitrary
-- attributes, provider keys, or automatic expiry are permitted in this table.
CREATE TABLE IF NOT EXISTS evaluation_scores (
    score_id UUID,
    job_id UUID,
    result_id UUID,
    dataset_id UUID,
    suite_id UUID,
    evaluator_id UUID,
    evaluator_version LowCardinality(String),
    evaluator_kind LowCardinality(String),
    component_id LowCardinality(String),
    component_version LowCardinality(String),
    capture_classification LowCardinality(String),
    evidence_status LowCardinality(String),
    score_value Nullable(Float64),
    outcome LowCardinality(Nullable(String)),
    observed_at DateTime64(3, 'UTC'),
    artifact_reference_id Nullable(UUID)
) ENGINE = ReplacingMergeTree
ORDER BY (job_id, result_id, score_id)
SETTINGS non_replicated_deduplication_window = 100000;

ALTER TABLE evaluation_scores
    MODIFY COLUMN IF EXISTS outcome LowCardinality(Nullable(String));

INSERT INTO observatory_migration_ledger(version, applied_at)
SELECT '0005_evaluation_scores', now64(3)
WHERE NOT EXISTS (
    SELECT 1 FROM observatory_migration_ledger
    WHERE version = '0005_evaluation_scores'
);
