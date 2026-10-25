-- AURA-0104: durable public admission identities.
-- This migration is forward-only and safe to rerun against a partially
-- upgraded Observatory.  It never stores content or owner identity.
ALTER TABLE observatory_evaluation_authorizations
    ADD COLUMN IF NOT EXISTS idempotency_key UUID;
ALTER TABLE observatory_evaluation_authorizations
    ADD COLUMN IF NOT EXISTS request_fingerprint CHAR(64);
ALTER TABLE observatory_evaluation_authorizations
    ADD COLUMN IF NOT EXISTS dataset_version VARCHAR(64);
ALTER TABLE observatory_evaluation_artifacts
    ADD COLUMN IF NOT EXISTS intended_use VARCHAR(32) NOT NULL DEFAULT 'input';
ALTER TABLE observatory_evaluation_artifacts
    ADD COLUMN IF NOT EXISTS artifact_version VARCHAR(64) NOT NULL DEFAULT '1.0.0';

CREATE UNIQUE INDEX IF NOT EXISTS observatory_evaluation_authorizations_idempotency_idx
    ON observatory_evaluation_authorizations(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
ALTER TABLE observatory_evaluation_authorizations
    DROP CONSTRAINT IF EXISTS observatory_evaluation_authorizations_fingerprint_ck;
ALTER TABLE observatory_evaluation_authorizations
    ADD CONSTRAINT observatory_evaluation_authorizations_fingerprint_ck
    CHECK (request_fingerprint IS NULL OR request_fingerprint ~ '^[0-9a-f]{64}$');

-- Existing 0010 already broadens this constraint on fresh installations.  The
-- guarded replacement also repairs installations which stopped at 0009.
ALTER TABLE observatory_evaluation_evaluators
    DROP CONSTRAINT IF EXISTS observatory_eval_evaluator_kind_ck;
ALTER TABLE observatory_evaluation_evaluators
ADD CONSTRAINT observatory_eval_evaluator_kind_ck
    CHECK (kind IN ('deterministic', 'local_judge', 'human_review'));

ALTER TABLE observatory_evaluation_definition_commands
    DROP CONSTRAINT IF EXISTS observatory_eval_definition_command_operation_ck;
ALTER TABLE observatory_evaluation_definition_commands
    ADD CONSTRAINT observatory_eval_definition_command_operation_ck CHECK (
        operation IN ('create_dataset', 'create_suite', 'create_local_judge_evaluator')
    );
