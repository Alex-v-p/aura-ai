-- AURA-0095 authoritative, content-free score provenance.
-- Nullable columns preserve legacy rows as explicitly partial/unavailable;
-- new writers must supply the trusted deployment markers before a score can
-- be projected as complete by observatory-v1.
ALTER TABLE evaluation_scores
    ADD COLUMN IF NOT EXISTS code_revision Nullable(String),
    ADD COLUMN IF NOT EXISTS image_digest Nullable(String),
    ADD COLUMN IF NOT EXISTS configuration_revision Nullable(String),
    ADD COLUMN IF NOT EXISTS provenance_component Nullable(String),
    ADD COLUMN IF NOT EXISTS provenance_version Nullable(String),
    ADD COLUMN IF NOT EXISTS provenance_digest Nullable(String),
    ADD COLUMN IF NOT EXISTS execution_provenance_json Nullable(String);

INSERT INTO observatory_migration_ledger(version, applied_at)
SELECT '0008_evaluation_api_provenance', now64(3)
WHERE NOT EXISTS (
    SELECT 1 FROM observatory_migration_ledger
    WHERE version = '0008_evaluation_api_provenance'
);
