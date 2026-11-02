-- AURA-0094 content-free exact local-judge score provenance.
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS judge_definition_id Nullable(UUID);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS judge_definition_version LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS model_name LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS model_version LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS model_digest Nullable(String);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS prompt_version LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS rubric_version LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS calibration_version LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS judge_seed Nullable(Int64);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS judge_budget_timeout_ms Nullable(UInt32);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS judge_budget_max_input_bytes Nullable(UInt32);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS judge_budget_max_output_bytes Nullable(UInt32);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS judge_budget_max_output_tokens Nullable(UInt32);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS judge_budget_max_context_tokens Nullable(UInt32);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS judge_budget_max_concurrency Nullable(UInt8);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS score_disposition LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS qualification_id Nullable(UUID);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS qualification_version LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS prompt_artifact_id Nullable(UUID);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS prompt_artifact_version LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS prompt_artifact_digest Nullable(String);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS rubric_artifact_id Nullable(UUID);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS rubric_artifact_version LowCardinality(Nullable(String));
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS rubric_artifact_digest Nullable(String);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS calibration_id Nullable(UUID);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS uncertainty Nullable(Float64);
ALTER TABLE evaluation_scores ADD COLUMN IF NOT EXISTS disagreement LowCardinality(Nullable(String));

INSERT INTO observatory_migration_ledger(version, applied_at)
SELECT '0007_local_judge_scores', now64(3)
WHERE NOT EXISTS (
    SELECT 1 FROM observatory_migration_ledger
    WHERE version = '0007_local_judge_scores'
);
