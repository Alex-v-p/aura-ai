-- Contiguous visibility fence for persisted ordering and SSE replay.
-- A sequence is allocated before ClickHouse insertion, but the watermark
-- advances only after that row is visible, so a later sequence cannot leak
-- through a snapshot while an earlier insert is still pending.
ALTER TABLE observatory_observation_receipts
    ADD COLUMN IF NOT EXISTS ingest_sequence BIGINT;

CREATE TABLE IF NOT EXISTS observatory_ingest_visibility (
    sequence BIGINT PRIMARY KEY,
    visible BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS observatory_ingest_watermark (
    watermark_key BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (watermark_key = TRUE),
    committed_sequence BIGINT NOT NULL DEFAULT 0
);

INSERT INTO observatory_ingest_watermark (watermark_key, committed_sequence)
VALUES (TRUE, 0)
ON CONFLICT (watermark_key) DO NOTHING;

-- Transactional allocator.  PostgreSQL sequences are deliberately retained
-- for legacy installations, but a row-locked allocator prevents a crash
-- between allocation and the ClickHouse insert from creating a permanent
-- visibility gap for new observations.
CREATE TABLE IF NOT EXISTS observatory_ingest_allocator (
    allocator_key BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (allocator_key = TRUE),
    next_sequence BIGINT NOT NULL DEFAULT 1
);

INSERT INTO observatory_ingest_allocator (allocator_key, next_sequence)
VALUES (TRUE, 1)
ON CONFLICT (allocator_key) DO NOTHING;
