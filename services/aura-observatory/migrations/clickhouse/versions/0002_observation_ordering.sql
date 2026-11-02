-- Forward-only ordering column used for stable pages and persisted SSE tailing.
ALTER TABLE observations ADD COLUMN IF NOT EXISTS ingest_sequence UInt64 DEFAULT 0;

CREATE TABLE IF NOT EXISTS run_projections (
    run_id String,
    last_seen_at AggregateFunction(max, DateTime64(3, 'UTC')),
    observation_count AggregateFunction(sum, UInt64)
) ENGINE = AggregatingMergeTree
ORDER BY run_id;

-- Rebuild the derived projection deterministically.  A multiquery request can
-- commit the INSERT before the migration ledger marker; truncating first
-- makes a retry safe even when that marker was not written.
TRUNCATE TABLE run_projections;

INSERT INTO run_projections
SELECT run_id, maxState(observed_at), sumState(toUInt64(1))
FROM observations
WHERE run_id != ''
GROUP BY run_id;
