-- AURA-0088 bounded comparison provenance.  Markers are nullable explicit
-- columns, never dimensions or labels.  This migration is safe on empty,
-- upgraded, and already-migrated installations.
ALTER TABLE observations ADD COLUMN IF NOT EXISTS image_version Nullable(String);
ALTER TABLE observations ADD COLUMN IF NOT EXISTS configuration_version Nullable(String);
ALTER TABLE observations ADD COLUMN IF NOT EXISTS model_id Nullable(String);
ALTER TABLE observations ADD COLUMN IF NOT EXISTS model_version Nullable(String);
ALTER TABLE observations ADD COLUMN IF NOT EXISTS agent_revision Nullable(String);
ALTER TABLE observations ADD COLUMN IF NOT EXISTS prompt_bundle_revision Nullable(String);
ALTER TABLE observations ADD COLUMN IF NOT EXISTS policy_revision Nullable(String);

INSERT INTO observatory_migration_ledger (version, applied_at)
SELECT '0004_comparison_markers', now64(3)
WHERE NOT EXISTS (
    SELECT 1 FROM observatory_migration_ledger
    WHERE version = '0004_comparison_markers'
);
