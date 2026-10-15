-- Forward-only legacy ordering backfill.  The migration runner records the
-- version in its service-owned ledger before retrying this statement, making
-- reruns safe for populated installations.  Automatic expiry is intentionally absent.
CREATE TABLE IF NOT EXISTS observatory_migration_ledger (
    version String,
    applied_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(applied_at)
ORDER BY version;

-- Existing rows receive a deterministic, content-free order key.  New rows
-- use the PostgreSQL contiguous visibility sequence.  This ALTER is
-- idempotent and is intentionally separate from aggregate projection DDL.
ALTER TABLE observations ADD COLUMN IF NOT EXISTS ingest_sequence UInt64 DEFAULT 0;

-- Materialize one deterministic order key for rows written before the
-- visibility fence existed. The INSERT is idempotent by observation id and
-- the subsequent mutation is rerunnable because only zero keys are updated.
CREATE TABLE IF NOT EXISTS observatory_legacy_ordering (
    observation_id String,
    ingest_sequence UInt64
) ENGINE = ReplacingMergeTree
ORDER BY observation_id;

INSERT INTO observatory_legacy_ordering (observation_id, ingest_sequence)
SELECT observation_id,
       row_number() OVER (ORDER BY observed_at ASC, observation_id ASC)
       + (SELECT ifNull(max(ingest_sequence), 0) FROM observations)
FROM observations
WHERE ingest_sequence = 0
  AND observation_id NOT IN (SELECT observation_id FROM observatory_legacy_ordering);

ALTER TABLE observations UPDATE ingest_sequence =
    (SELECT ingest_sequence FROM observatory_legacy_ordering
     WHERE observatory_legacy_ordering.observation_id = observations.observation_id)
WHERE ingest_sequence = 0
SETTINGS mutations_sync = 2;

INSERT INTO observatory_migration_ledger (version, applied_at)
SELECT '0003_legacy_ordering_ledger', now64(3)
WHERE NOT EXISTS (
    SELECT 1 FROM observatory_migration_ledger
    WHERE version = '0003_legacy_ordering_ledger'
);
