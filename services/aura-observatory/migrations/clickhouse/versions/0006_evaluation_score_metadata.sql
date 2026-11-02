-- AURA-0093 bounded score metadata.  Forward-only: migration 0005 remains
-- immutable and no evaluation content is added.
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS case_id Nullable(UUID);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS metric LowCardinality(String) DEFAULT 'evaluation.score';
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS failure_code LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS evidence_reference_id Nullable(String);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS reproducibility LowCardinality(String) DEFAULT 'deterministic-v1';
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS threshold_applicable Nullable(UInt8);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS threshold_reference Nullable(String);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS threshold_value Nullable(Float64);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS threshold_operator LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS threshold_status LowCardinality(Nullable(String));

INSERT INTO observatory_migration_ledger(version, applied_at)
SELECT '0006_evaluation_score_metadata', now64(3)
WHERE NOT EXISTS (
    SELECT 1 FROM observatory_migration_ledger
    WHERE version = '0006_evaluation_score_metadata'
);
