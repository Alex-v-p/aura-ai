ALTER TABLE observatory_ingest_counters
    ADD COLUMN IF NOT EXISTS last_accepted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_failure_at TIMESTAMPTZ;
