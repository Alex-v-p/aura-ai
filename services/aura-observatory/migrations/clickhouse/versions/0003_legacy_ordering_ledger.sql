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

-- Recreate the transient lookup so installations left with the original
-- ReplacingMergeTree helper recover before the 25.8-compatible mutation runs.
-- The helper contains only observation IDs and their derived order keys.
DROP TABLE IF EXISTS observatory_legacy_ordering;

CREATE TABLE observatory_legacy_ordering (
    observation_id String,
    ingest_sequence UInt64
) ENGINE = Join(ANY, LEFT, observation_id);

-- Materialize one deterministic order key for rows written before the
-- visibility fence existed. The helper is rebuilt on every retry, while only
-- zero-valued source rows are selected and updated.
INSERT INTO observatory_legacy_ordering (observation_id, ingest_sequence)
SELECT observation_id,
       row_number() OVER (ORDER BY observed_at ASC, observation_id ASC)
       + (SELECT ifNull(max(ingest_sequence), 0) FROM observations)
FROM observations
WHERE ingest_sequence = 0;

-- ClickHouse 25.8 does not resolve a correlated scalar subquery in an ALTER
-- mutation. joinGet reads the freshly populated in-memory lookup instead.
ALTER TABLE observations UPDATE ingest_sequence =
    joinGet('observatory_legacy_ordering', 'ingest_sequence', observation_id)
WHERE ingest_sequence = 0
SETTINGS mutations_sync = 2;

-- The lookup is migration scratch state, not a catalogue or retention store.
-- Dropping it also makes a retry recover cleanly after a post-mutation failure.
DROP TABLE IF EXISTS observatory_legacy_ordering;

INSERT INTO observatory_migration_ledger (version, applied_at)
SELECT '0003_legacy_ordering_ledger', now64(3)
WHERE NOT EXISTS (
    SELECT 1 FROM observatory_migration_ledger
    WHERE version = '0003_legacy_ordering_ledger'
);
