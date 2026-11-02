-- Forward-only, rerunnable retention progress extensions.
ALTER TABLE observatory_retention_actions
    ADD COLUMN IF NOT EXISTS command_fingerprint VARCHAR(256),
    ADD COLUMN IF NOT EXISTS target_observation_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS target_receipt_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS target_run_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS deletion_phase VARCHAR(32) NOT NULL DEFAULT 'queued';

CREATE UNIQUE INDEX IF NOT EXISTS observatory_retention_actions_fingerprint_key
    ON observatory_retention_actions(idempotency_key, command_fingerprint)
    WHERE idempotency_key IS NOT NULL AND command_fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS observatory_retention_targets (
    action_id UUID NOT NULL REFERENCES observatory_retention_actions(action_id),
    observation_id VARCHAR(128) NOT NULL,
    run_id VARCHAR(128),
    phase VARCHAR(32) NOT NULL DEFAULT 'targeted',
    PRIMARY KEY (action_id, observation_id)
);

CREATE INDEX IF NOT EXISTS observatory_retention_targets_run_key
    ON observatory_retention_targets(action_id, run_id, observation_id)
    WHERE run_id IS NOT NULL;
