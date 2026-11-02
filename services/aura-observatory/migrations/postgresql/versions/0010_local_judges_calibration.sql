-- AURA-0094 immutable local judge and calibration state.
-- Only bounded metadata and opaque artifact references are persisted here.

CREATE TABLE IF NOT EXISTS observatory_evaluation_local_work (
    work_id UUID PRIMARY KEY,
    idempotency_key UUID NOT NULL UNIQUE,
    source_job_id UUID,
    source_attempt_id UUID,
    kind VARCHAR(16) NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    payload_json JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'queued',
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    worker_id VARCHAR(128),
    attempts SMALLINT NOT NULL DEFAULT 0,
    result_json JSONB,
    failure_code VARCHAR(32),
    completed_at TIMESTAMPTZ,
    CONSTRAINT observatory_local_work_kind_ck CHECK (kind IN ('judge', 'calibration')),
    CONSTRAINT observatory_local_work_status_ck CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    CONSTRAINT observatory_local_work_fingerprint_ck CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT observatory_local_work_attempts_ck CHECK (attempts BETWEEN 0 AND 10)
);
ALTER TABLE observatory_evaluation_local_work
    ADD COLUMN IF NOT EXISTS source_job_id UUID;
ALTER TABLE observatory_evaluation_local_work
    ADD COLUMN IF NOT EXISTS source_attempt_id UUID;
CREATE INDEX IF NOT EXISTS observatory_local_work_due_idx
    ON observatory_evaluation_local_work(status, available_at, lease_expires_at, work_id);
CREATE INDEX IF NOT EXISTS observatory_local_work_source_idx
    ON observatory_evaluation_local_work(source_job_id, source_attempt_id, kind);

-- AURA-0093 created the evaluator catalogue with deterministic-only rows.
-- Observatory local judges are an explicitly admitted evaluator kind; retain
-- the old constraint only for upgraded databases that have no incompatible
-- rows, then widen it to the versioned evaluation-v1 set.
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_evaluation_evaluators
    DROP CONSTRAINT IF EXISTS observatory_eval_evaluator_kind_ck;
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_evaluation_evaluators
    ADD CONSTRAINT observatory_eval_evaluator_kind_ck
    CHECK (kind IN ('deterministic', 'local_judge', 'human_review'));

CREATE TABLE IF NOT EXISTS observatory_evaluation_owner_actions (
    authorization_record_id UUID PRIMARY KEY,
    action VARCHAR(24) NOT NULL,
    resource_id UUID NOT NULL,
    scope_fingerprint CHAR(64) NOT NULL,
    idempotency_key UUID NOT NULL UNIQUE,
    revoked_at TIMESTAMPTZ,
    authorized_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT observatory_owner_action_kind_ck CHECK (
        action IN ('calibration_label', 'human_review', 'qualification', 'revocation')
    ),
    CONSTRAINT observatory_owner_action_scope_ck CHECK (scope_fingerprint ~ '^[0-9a-f]{64}$')
);

ALTER TABLE observatory_evaluation_artifacts
    ADD COLUMN IF NOT EXISTS intended_use VARCHAR(16) NOT NULL DEFAULT 'input'
    CHECK (intended_use IN ('prompt', 'rubric', 'input', 'comment'));

ALTER TABLE observatory_evaluation_artifacts
    ADD COLUMN IF NOT EXISTS artifact_version VARCHAR(64) NOT NULL DEFAULT '1.0.0'
    CHECK (artifact_version ~ '^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$');

CREATE TABLE IF NOT EXISTS observatory_local_judge_definitions (
    judge_id UUID NOT NULL,
    judge_version VARCHAR(64) NOT NULL,
    evaluator_id UUID,
    evaluator_version VARCHAR(64),
    model_name VARCHAR(128) NOT NULL,
    model_version VARCHAR(64) NOT NULL,
    model_digest CHAR(64) NOT NULL,
    instruction_version VARCHAR(64) NOT NULL,
    rubric_version VARCHAR(64) NOT NULL,
    calibration_version VARCHAR(64) NOT NULL,
    calibration_id UUID,
    instruction_artifact_id UUID NOT NULL,
    rubric_artifact_id UUID NOT NULL,
    label_ids VARCHAR[] NOT NULL,
    seed INTEGER NOT NULL DEFAULT 0,
    temperature DOUBLE PRECISION NOT NULL DEFAULT 0,
    timeout_seconds DOUBLE PRECISION NOT NULL,
    max_output_bytes INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    max_input_bytes INTEGER NOT NULL,
    context_tokens INTEGER NOT NULL,
    max_concurrency SMALLINT NOT NULL,
    metric VARCHAR(128) NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    definition_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (judge_id, judge_version),
    CONSTRAINT observatory_judge_digest_ck CHECK (model_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT observatory_judge_seed_ck CHECK (seed BETWEEN 0 AND 2147483647 AND temperature = 0),
    CONSTRAINT observatory_judge_limits_ck CHECK (timeout_seconds > 0 AND timeout_seconds <= 120 AND max_output_bytes BETWEEN 1 AND 8192 AND max_output_tokens BETWEEN 1 AND 2048 AND max_input_bytes BETWEEN 1 AND 1048576 AND context_tokens BETWEEN 1 AND 16384 AND max_concurrency BETWEEN 1 AND 2),
    CONSTRAINT observatory_judge_fingerprint_ck CHECK (fingerprint ~ '^[0-9a-f]{64}$')
);

-- Existing 0010 rows may predate independent evaluator identity.  Keep those
-- rows readable for migration/inspection, while production INSERT paths and
-- executable request decoding require both values.
ALTER TABLE observatory_local_judge_definitions
    ADD COLUMN IF NOT EXISTS evaluator_id UUID,
    ADD COLUMN IF NOT EXISTS evaluator_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS calibration_id UUID;
CREATE INDEX IF NOT EXISTS observatory_local_judge_definition_evaluator_idx
    ON observatory_local_judge_definitions(evaluator_id, evaluator_version);

CREATE TABLE IF NOT EXISTS observatory_local_judge_labels (
    label_id UUID PRIMARY KEY,
    case_id UUID NOT NULL,
    judge_id UUID NOT NULL,
    judge_version VARCHAR(64) NOT NULL,
    instruction_version VARCHAR(64) NOT NULL,
    rubric_version VARCHAR(64) NOT NULL,
    calibration_version VARCHAR(64) NOT NULL,
    reference_score DOUBLE PRECISION NOT NULL,
    reference_label VARCHAR(16) NOT NULL,
    judge_score DOUBLE PRECISION,
    judge_label VARCHAR(16),
    comment_artifact_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    provenance VARCHAR(32) NOT NULL DEFAULT 'owner_label',
    UNIQUE (case_id, judge_id, judge_version, calibration_version),
    FOREIGN KEY (judge_id, judge_version) REFERENCES observatory_local_judge_definitions(judge_id, judge_version) ON DELETE RESTRICT,
    CONSTRAINT observatory_judge_label_score_ck CHECK (reference_score BETWEEN 0 AND 1 AND (judge_score IS NULL OR judge_score BETWEEN 0 AND 1)),
    CONSTRAINT observatory_judge_label_kind_ck CHECK (reference_label IN ('accepted', 'rejected', 'needs_review', 'pass', 'fail', 'inconclusive') AND (judge_label IS NULL OR judge_label IN ('accepted', 'rejected', 'needs_review', 'pass', 'fail', 'inconclusive')))
);

CREATE TABLE IF NOT EXISTS observatory_local_judge_reviews (
    review_id UUID PRIMARY KEY,
    case_id UUID NOT NULL,
    result_id UUID NOT NULL,
    judge_id UUID NOT NULL,
    judge_version VARCHAR(64) NOT NULL,
    instruction_version VARCHAR(64) NOT NULL,
    rubric_version VARCHAR(64) NOT NULL,
    calibration_version VARCHAR(64) NOT NULL,
    label VARCHAR(16) NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    disagreement VARCHAR(24) NOT NULL,
    comment_artifact_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    provenance VARCHAR(32) NOT NULL DEFAULT 'owner_review',
    UNIQUE (result_id, calibration_version),
    FOREIGN KEY (judge_id, judge_version) REFERENCES observatory_local_judge_definitions(judge_id, judge_version) ON DELETE RESTRICT,
    CONSTRAINT observatory_judge_review_score_ck CHECK (score BETWEEN 0 AND 1),
    CONSTRAINT observatory_judge_review_label_ck CHECK (label IN ('accepted', 'rejected', 'needs_review', 'pass', 'fail', 'inconclusive')),
    CONSTRAINT observatory_judge_review_disagreement_ck CHECK (disagreement IN ('none', 'within_tolerance', 'score_disagreement', 'outcome_disagreement', 'score', 'label', 'evidence', 'insufficient_evidence'))
);

ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_labels
    ADD COLUMN IF NOT EXISTS calibration_id UUID,
    ADD COLUMN IF NOT EXISTS result_id UUID,
    ADD COLUMN IF NOT EXISTS reference_outcome VARCHAR(16);
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_labels
    ADD COLUMN IF NOT EXISTS record_fingerprint CHAR(64);
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_reviews
    ADD COLUMN IF NOT EXISTS calibration_id UUID,
    ADD COLUMN IF NOT EXISTS score_id UUID,
    ADD COLUMN IF NOT EXISTS review_label VARCHAR(16),
    ADD COLUMN IF NOT EXISTS review_outcome VARCHAR(16);
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_reviews
    ADD COLUMN IF NOT EXISTS record_fingerprint CHAR(64);

-- Contract-shaped opaque comment bindings.  Legacy rows remain readable, but
-- new production writes must provide the complete binding alongside the UUID.
ALTER TABLE observatory_local_judge_labels
    ADD COLUMN IF NOT EXISTS comment_artifact_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS comment_artifact_digest CHAR(64);
ALTER TABLE observatory_local_judge_reviews
    ADD COLUMN IF NOT EXISTS comment_artifact_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS comment_artifact_digest CHAR(64);

-- Contract-shaped outcome fields are nullable only for legacy rows. New
-- writes are validated by the strict domain adapter and must provide the
-- closed evaluation-v1 values.
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_labels
    DROP CONSTRAINT IF EXISTS observatory_judge_label_reference_outcome_ck;
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_labels
    ADD CONSTRAINT observatory_judge_label_reference_outcome_ck CHECK (
        reference_outcome IS NULL OR reference_outcome IN ('pass', 'fail', 'inconclusive')
    );
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_reviews
    DROP CONSTRAINT IF EXISTS observatory_judge_review_outcome_ck;
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_reviews
    ADD CONSTRAINT observatory_judge_review_outcome_ck CHECK (
        review_outcome IS NULL OR review_outcome IN ('pass', 'fail', 'inconclusive')
    );

CREATE TABLE IF NOT EXISTS observatory_local_judge_qualifications (
    qualification_id UUID PRIMARY KEY,
    suite_id UUID NOT NULL,
    suite_version VARCHAR(64) NOT NULL,
    dataset_id UUID NOT NULL,
    dataset_version VARCHAR(64) NOT NULL,
    evaluator_id UUID NOT NULL,
    evaluator_version VARCHAR(64) NOT NULL,
    judge_id UUID NOT NULL,
    judge_version VARCHAR(64) NOT NULL,
    instruction_version VARCHAR(64) NOT NULL,
    rubric_version VARCHAR(64) NOT NULL,
    calibration_version VARCHAR(64) NOT NULL,
    qualified BOOLEAN NOT NULL DEFAULT TRUE,
    reason VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ,
    state VARCHAR(16) NOT NULL DEFAULT 'qualified',
    action_mode VARCHAR(32) NOT NULL DEFAULT 'manual_owner_action',
    score_disposition VARCHAR(24) NOT NULL DEFAULT 'advisory_only',
    retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    UNIQUE (suite_id, suite_version, dataset_id, dataset_version, evaluator_id, evaluator_version, judge_id, judge_version, instruction_version, rubric_version, calibration_version),
    FOREIGN KEY (judge_id, judge_version) REFERENCES observatory_local_judge_definitions(judge_id, judge_version) ON DELETE RESTRICT,
    CONSTRAINT observatory_judge_qualification_state_ck CHECK (state IN ('qualified', 'revoked', 'denied', 'inconclusive') AND action_mode IN ('manual', 'manual_owner_action') AND score_disposition IN ('advisory_only', 'qualified_advisory') AND retention_mode = 'manual_only')
);

-- Add forward-compatible qualification provenance only after the base table
-- exists.  This ordering is valid on a fresh schema and on upgrades.
ALTER TABLE observatory_local_judge_qualifications
    ADD COLUMN IF NOT EXISTS model_name VARCHAR(128),
    ADD COLUMN IF NOT EXISTS calibration_id UUID,
    ADD COLUMN IF NOT EXISTS model_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS model_digest CHAR(64),
    ADD COLUMN IF NOT EXISTS prompt_artifact_id UUID,
    ADD COLUMN IF NOT EXISTS rubric_artifact_id UUID,
    ADD COLUMN IF NOT EXISTS calibration_eligibility VARCHAR(16) NOT NULL DEFAULT 'eligible',
    ADD COLUMN IF NOT EXISTS qualified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS revocation_record_id UUID,
    ADD COLUMN IF NOT EXISTS record_fingerprint CHAR(64);

-- These base tables must exist before any provenance ALTER below.  Keep the
-- declarations idempotent so a fresh database and an exact rerun share the
-- same ordering and upgrade behaviour.
CREATE TABLE IF NOT EXISTS observatory_local_judge_calibration_definitions (
    calibration_id UUID NOT NULL,
    calibration_version VARCHAR(64) NOT NULL,
    dataset_id UUID NOT NULL,
    dataset_version VARCHAR(64) NOT NULL,
    suite_id UUID NOT NULL,
    suite_version VARCHAR(64) NOT NULL,
    evaluator_id UUID NOT NULL,
    evaluator_version VARCHAR(64) NOT NULL,
    judge_id UUID NOT NULL,
    judge_version VARCHAR(64) NOT NULL,
    definition_json JSONB NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (calibration_id, calibration_version),
    FOREIGN KEY (judge_id, judge_version) REFERENCES observatory_local_judge_definitions(judge_id, judge_version) ON DELETE RESTRICT,
    CONSTRAINT observatory_judge_calibration_fingerprint_ck CHECK (fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS observatory_local_judge_calibration_results (
    calibration_result_id UUID PRIMARY KEY,
    calibration_id UUID NOT NULL,
    calibration_version VARCHAR(64) NOT NULL,
    judge_id UUID NOT NULL,
    judge_version VARCHAR(64) NOT NULL,
    sample_count INTEGER NOT NULL,
    agreement_tolerance DOUBLE PRECISION NOT NULL DEFAULT 0.1,
    agreement_rate DOUBLE PRECISION NOT NULL,
    mean_absolute_error DOUBLE PRECISION NOT NULL,
    disagreement_rate DOUBLE PRECISION NOT NULL,
    eligibility VARCHAR(16) NOT NULL,
    result_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (calibration_id, calibration_version),
    FOREIGN KEY (calibration_id, calibration_version) REFERENCES observatory_local_judge_calibration_definitions(calibration_id, calibration_version) ON DELETE RESTRICT,
    CONSTRAINT observatory_judge_calibration_result_eligibility_ck CHECK (eligibility IN ('eligible', 'ineligible')),
    CONSTRAINT observatory_judge_calibration_result_bounds_ck CHECK (sample_count BETWEEN 0 AND 100000 AND agreement_rate BETWEEN 0 AND 1 AND mean_absolute_error BETWEEN 0 AND 1 AND disagreement_rate BETWEEN 0 AND 1)
);

ALTER TABLE observatory_local_judge_calibration_results
    ADD COLUMN IF NOT EXISTS model_name VARCHAR(128),
    ADD COLUMN IF NOT EXISTS model_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS model_digest CHAR(64),
    ADD COLUMN IF NOT EXISTS prompt_artifact_id UUID,
    ADD COLUMN IF NOT EXISTS prompt_artifact_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS prompt_artifact_digest CHAR(64),
    ADD COLUMN IF NOT EXISTS rubric_artifact_id UUID,
    ADD COLUMN IF NOT EXISTS rubric_artifact_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS rubric_artifact_digest CHAR(64),
    ADD COLUMN IF NOT EXISTS seed INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS budget_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS provenance_component VARCHAR(96),
    ADD COLUMN IF NOT EXISTS provenance_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS provenance_digest CHAR(64);

ALTER TABLE observatory_local_judge_qualifications
    ADD COLUMN IF NOT EXISTS prompt_artifact_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS prompt_artifact_digest CHAR(64),
    ADD COLUMN IF NOT EXISTS rubric_artifact_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS rubric_artifact_digest CHAR(64),
    ADD COLUMN IF NOT EXISTS seed INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS budget_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS provenance_component VARCHAR(96),
    ADD COLUMN IF NOT EXISTS provenance_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS provenance_digest CHAR(64);

-- Older AURA-0093/early AURA-0094 schemas used VARCHAR(16) for the
-- qualification action mode.  Widen before the canonical owner-action
-- backfill so upgrades cannot fail with StringDataRightTruncation.  The
-- operation is lossless and is safe to repeat on fresh or upgraded schemas.
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_qualifications
    ALTER COLUMN action_mode TYPE VARCHAR(32);

UPDATE observatory_local_judge_qualifications
SET action_mode='manual_owner_action'
WHERE action_mode='manual';
UPDATE observatory_local_judge_qualifications
SET score_disposition=CASE WHEN state='qualified' THEN 'calibrated_enabled' ELSE 'advisory_only' END
WHERE score_disposition IN ('advisory_only', 'qualified_advisory');
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_qualifications
    ALTER COLUMN action_mode SET DEFAULT 'manual_owner_action',
    ALTER COLUMN score_disposition SET DEFAULT 'calibrated_enabled';
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_qualifications
    DROP CONSTRAINT IF EXISTS observatory_judge_qualification_state_ck;
ALTER TABLE /* IF NOT EXISTS: idempotent upgrade */ observatory_local_judge_qualifications
    ADD CONSTRAINT observatory_judge_qualification_state_ck CHECK (
        state IN ('qualified', 'revoked')
        AND action_mode = 'manual_owner_action'
        AND score_disposition IN ('advisory_only', 'calibrated_enabled')
        AND retention_mode = 'manual_only'
    );

CREATE TABLE IF NOT EXISTS observatory_local_judge_ensembles (
    ensemble_id UUID NOT NULL,
    ensemble_version VARCHAR(64) NOT NULL,
    member_ids UUID[] NOT NULL,
    member_weights BIGINT[] NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    definition_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ensemble_id, ensemble_version),
    CONSTRAINT observatory_judge_ensemble_fingerprint_ck CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT observatory_judge_ensemble_size_ck CHECK (cardinality(member_ids) BETWEEN 2 AND 16 AND cardinality(member_ids) = cardinality(member_weights))
);

CREATE TABLE IF NOT EXISTS observatory_local_judge_calibration_definitions (
    calibration_id UUID NOT NULL,
    calibration_version VARCHAR(64) NOT NULL,
    dataset_id UUID NOT NULL,
    dataset_version VARCHAR(64) NOT NULL,
    suite_id UUID NOT NULL,
    suite_version VARCHAR(64) NOT NULL,
    evaluator_id UUID NOT NULL,
    evaluator_version VARCHAR(64) NOT NULL,
    judge_id UUID NOT NULL,
    judge_version VARCHAR(64) NOT NULL,
    definition_json JSONB NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (calibration_id, calibration_version),
    FOREIGN KEY (judge_id, judge_version) REFERENCES observatory_local_judge_definitions(judge_id, judge_version) ON DELETE RESTRICT,
    CONSTRAINT observatory_judge_calibration_fingerprint_ck CHECK (fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS observatory_local_judge_calibration_results (
    calibration_result_id UUID PRIMARY KEY,
    calibration_id UUID NOT NULL,
    calibration_version VARCHAR(64) NOT NULL,
    judge_id UUID NOT NULL,
    judge_version VARCHAR(64) NOT NULL,
    sample_count INTEGER NOT NULL,
    agreement_tolerance DOUBLE PRECISION NOT NULL DEFAULT 0.1,
    agreement_rate DOUBLE PRECISION NOT NULL,
    mean_absolute_error DOUBLE PRECISION NOT NULL,
    disagreement_rate DOUBLE PRECISION NOT NULL,
    eligibility VARCHAR(16) NOT NULL,
    result_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (calibration_id, calibration_version),
    FOREIGN KEY (calibration_id, calibration_version) REFERENCES observatory_local_judge_calibration_definitions(calibration_id, calibration_version) ON DELETE RESTRICT,
    CONSTRAINT observatory_judge_calibration_result_eligibility_ck CHECK (eligibility IN ('eligible', 'ineligible')),
    CONSTRAINT observatory_judge_calibration_result_bounds_ck CHECK (sample_count BETWEEN 0 AND 100000 AND agreement_rate BETWEEN 0 AND 1 AND mean_absolute_error BETWEEN 0 AND 1 AND disagreement_rate BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS observatory_local_judge_calibration_claims (
    calibration_id UUID NOT NULL,
    calibration_version VARCHAR(64) NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'claimed',
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (calibration_id, calibration_version),
    CONSTRAINT observatory_judge_calibration_claim_status_ck CHECK (status IN ('claimed', 'completed', 'failed')),
    CONSTRAINT observatory_judge_calibration_claim_fingerprint_ck CHECK (fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS observatory_local_judge_execution_claims (
    idempotency_key UUID PRIMARY KEY,
    request_fingerprint CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    result_json JSONB,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    failure_code VARCHAR(32),
    CONSTRAINT observatory_judge_claim_status_ck CHECK (status IN ('claimed', 'completed', 'failed')),
    CONSTRAINT observatory_judge_claim_fingerprint_ck CHECK (request_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS observatory_local_judge_audits (
    audit_id UUID PRIMARY KEY,
    resource_type VARCHAR(24) NOT NULL,
    resource_id UUID NOT NULL,
    operation VARCHAR(24) NOT NULL,
    outcome VARCHAR(24) NOT NULL,
    failure_code VARCHAR(32),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT observatory_judge_audit_resource_ck CHECK (resource_type IN ('judge', 'label', 'review', 'qualification', 'ensemble')),
    CONSTRAINT observatory_judge_audit_operation_ck CHECK (operation IN ('create', 'duplicate', 'conflict', 'revoke', 'qualify', 'reject')),
    CONSTRAINT observatory_judge_audit_outcome_ck CHECK (outcome IN ('accepted', 'duplicate', 'rejected'))
);

CREATE INDEX IF NOT EXISTS observatory_judge_labels_lookup
    ON observatory_local_judge_labels(judge_id, judge_version, calibration_version, case_id);
CREATE INDEX IF NOT EXISTS observatory_judge_reviews_lookup
    ON observatory_local_judge_reviews(judge_id, judge_version, calibration_version, created_at);
CREATE INDEX IF NOT EXISTS observatory_judge_qualifications_lookup
    ON observatory_local_judge_qualifications(suite_id, dataset_id, evaluator_id, qualified, revoked_at);

-- Forward-compatible additions for contract-shaped idempotency and owner
-- authorization. Existing Observatory deployments receive these columns
-- without rewriting earlier migrations.
ALTER TABLE observatory_local_judge_labels
    ADD COLUMN IF NOT EXISTS label_version VARCHAR(64) NOT NULL DEFAULT '1.0.0',
    ADD COLUMN IF NOT EXISTS idempotency_key UUID,
    ADD COLUMN IF NOT EXISTS authorization_record_id UUID,
    ADD COLUMN IF NOT EXISTS retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    ADD COLUMN IF NOT EXISTS provenance VARCHAR(32) NOT NULL DEFAULT 'owner_label';
ALTER TABLE observatory_local_judge_reviews
    ADD COLUMN IF NOT EXISTS review_version VARCHAR(64) NOT NULL DEFAULT '1.0.0',
    ADD COLUMN IF NOT EXISTS idempotency_key UUID,
    ADD COLUMN IF NOT EXISTS authorization_record_id UUID,
    ADD COLUMN IF NOT EXISTS retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    ADD COLUMN IF NOT EXISTS provenance VARCHAR(32) NOT NULL DEFAULT 'owner_review';
ALTER TABLE observatory_local_judge_qualifications
    ADD COLUMN IF NOT EXISTS qualification_version VARCHAR(64) NOT NULL DEFAULT '1.0.0',
    ADD COLUMN IF NOT EXISTS idempotency_key UUID,
    ADD COLUMN IF NOT EXISTS authorization_record_id UUID,
    ADD COLUMN IF NOT EXISTS calibration_result_id UUID;
ALTER TABLE observatory_local_judge_qualifications
    ADD COLUMN IF NOT EXISTS state VARCHAR(16) NOT NULL DEFAULT 'qualified',
    ADD COLUMN IF NOT EXISTS action_mode VARCHAR(32) NOT NULL DEFAULT 'manual_owner_action',
    ADD COLUMN IF NOT EXISTS score_disposition VARCHAR(24) NOT NULL DEFAULT 'advisory_only',
    ADD COLUMN IF NOT EXISTS retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only';
CREATE UNIQUE INDEX IF NOT EXISTS observatory_judge_label_idempotency
    ON observatory_local_judge_labels(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS observatory_judge_review_idempotency
    ON observatory_local_judge_reviews(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS observatory_judge_qualification_idempotency
    ON observatory_local_judge_qualifications(idempotency_key) WHERE idempotency_key IS NOT NULL;
