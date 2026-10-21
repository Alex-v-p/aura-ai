-- AURA-0096 immutable experiment, baseline, promotion, and regression metadata.
-- Forward-only and rerunnable.  No evaluation content or owner identity is
-- stored; result/score references remain opaque identities.
CREATE TABLE IF NOT EXISTS observatory_evaluation_experiments (
    experiment_id UUID NOT NULL,
    experiment_version VARCHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'active',
    suite_id UUID NOT NULL,
    suite_version VARCHAR(64) NOT NULL,
    variants_json JSONB NOT NULL,
    cohorts_json JSONB NOT NULL,
    capture_classification VARCHAR(16) NOT NULL,
    retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    provenance_json JSONB NOT NULL,
    resource_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (experiment_id, experiment_version),
    CONSTRAINT observatory_experiment_state_ck CHECK (state IN ('active', 'superseded', 'deleted')),
    CONSTRAINT observatory_experiment_capture_ck CHECK (capture_classification IN ('metadata_only', 'hashed', 'redacted', 'sampled_content', 'full_content')),
    CONSTRAINT observatory_experiment_retention_ck CHECK (retention_mode = 'manual_only')
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_experiments_suite_idx
    ON observatory_evaluation_experiments (suite_id, suite_version, created_at DESC);

-- Public definition ownership resolves suite -> dataset without coupling the
-- experiment deletion path to definition tables.  This index permits dataset
-- retention to remove standalone experiments that have no result receipts.
CREATE TABLE IF NOT EXISTS observatory_evaluation_experiment_dataset_bindings (
    experiment_id UUID NOT NULL,
    experiment_version VARCHAR(64) NOT NULL,
    dataset_id UUID NOT NULL,
    dataset_version VARCHAR(64) NOT NULL,
    PRIMARY KEY (experiment_id, experiment_version)
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_experiment_dataset_idx
    ON observatory_evaluation_experiment_dataset_bindings (dataset_id, dataset_version, experiment_id);
ALTER TABLE observatory_evaluation_experiments /* if not exists */
    DROP CONSTRAINT IF EXISTS observatory_experiment_state_ck;
ALTER TABLE observatory_evaluation_experiments /* if not exists */
    ADD CONSTRAINT observatory_experiment_state_ck
    CHECK (state IN ('active', 'superseded', 'deleted'));

CREATE TABLE IF NOT EXISTS observatory_evaluation_baselines (
    baseline_id UUID NOT NULL,
    baseline_version VARCHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'candidate',
    experiment_id UUID NOT NULL,
    experiment_version VARCHAR(64) NOT NULL,
    suite_id UUID NOT NULL,
    suite_version VARCHAR(64) NOT NULL,
    variant_id VARCHAR(128) NOT NULL,
    cohort_ids_json JSONB NOT NULL,
    source_result_ids_json JSONB NOT NULL,
    source_result_from TIMESTAMPTZ NOT NULL,
    source_result_to TIMESTAMPTZ NOT NULL,
    thresholds_json JSONB NOT NULL,
    capture_classification VARCHAR(16) NOT NULL,
    reproducibility_json JSONB NOT NULL,
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    promotion_mode VARCHAR(32) NOT NULL DEFAULT 'manual_owner_action',
    manual_promotion_record_id UUID,
    promoted_at TIMESTAMPTZ,
    retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    provenance_json JSONB NOT NULL,
    resource_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (baseline_id, baseline_version),
    CONSTRAINT observatory_baseline_state_ck CHECK (state IN ('candidate', 'promoted')),
    CONSTRAINT observatory_baseline_immutable_ck CHECK (immutable = TRUE),
    CONSTRAINT observatory_baseline_mode_ck CHECK (promotion_mode = 'manual_owner_action'),
    CONSTRAINT observatory_baseline_retention_ck CHECK (retention_mode = 'manual_only'),
    CONSTRAINT observatory_baseline_promotion_ck CHECK ((state = 'candidate' AND manual_promotion_record_id IS NULL AND promoted_at IS NULL) OR (state = 'promoted' AND manual_promotion_record_id IS NOT NULL AND promoted_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_baselines_experiment_idx
    ON observatory_evaluation_baselines (experiment_id, experiment_version, state, created_at DESC);

CREATE TABLE IF NOT EXISTS observatory_evaluation_baseline_promotions (
    idempotency_key VARCHAR(128) PRIMARY KEY,
    baseline_id UUID NOT NULL,
    baseline_version VARCHAR(64) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    replay_json JSONB,
    completed_at TIMESTAMPTZ,
    authorization_record_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (baseline_id, baseline_version),
    CONSTRAINT observatory_baseline_promotion_fingerprint_ck CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT observatory_baseline_promotion_status_ck CHECK (status IN ('pending', 'completed'))
);

ALTER TABLE observatory_evaluation_baseline_promotions /* if not exists */
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS lease_token UUID,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS replay_json JSONB,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

-- Older development databases used a baseline-id-only uniqueness fence.  The
-- immutable contract permits a new version of the same baseline identity, so
-- make the upgrade deterministic as well as fresh-install safe.
ALTER TABLE observatory_evaluation_baseline_promotions /* if not exists */
    DROP CONSTRAINT IF EXISTS observatory_evaluation_baseline_promotions_baseline_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS observatory_evaluation_baseline_promotions_identity_idx
    ON observatory_evaluation_baseline_promotions (baseline_id, baseline_version);

CREATE TABLE IF NOT EXISTS observatory_evaluation_regressions (
    regression_id UUID NOT NULL,
    regression_version VARCHAR(64) NOT NULL,
    experiment_id UUID NOT NULL,
    experiment_version VARCHAR(64) NOT NULL,
    baseline_id UUID NOT NULL,
    baseline_version VARCHAR(64) NOT NULL,
    variant_id VARCHAR(128) NOT NULL,
    cohort_ids_json JSONB NOT NULL,
    baseline_result_ids_json JSONB NOT NULL,
    candidate_result_ids_json JSONB NOT NULL,
    comparisons_json JSONB NOT NULL,
    decision VARCHAR(16) NOT NULL,
    evidence_state VARCHAR(16) NOT NULL,
    evidence_json JSONB NOT NULL,
    attribution_json JSONB NOT NULL,
    resource_json JSONB NOT NULL,
    capture_policy VARCHAR(16) NOT NULL DEFAULT 'metadata_only',
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (regression_id, regression_version),
    CONSTRAINT observatory_regression_decision_ck CHECK (decision IN ('regressed', 'unchanged', 'inconclusive', 'unavailable', 'incompatible')),
    CONSTRAINT observatory_regression_evidence_ck CHECK (evidence_state IN ('complete', 'partial', 'insufficient', 'missing', 'incompatible', 'late')),
    CONSTRAINT observatory_regression_capture_ck CHECK (capture_policy = 'metadata_only')
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_regressions_experiment_idx
    ON observatory_evaluation_regressions (experiment_id, experiment_version, created_at DESC);
CREATE INDEX IF NOT EXISTS observatory_evaluation_regressions_baseline_idx
    ON observatory_evaluation_regressions (baseline_id, baseline_version, created_at DESC);

-- Durable reconciliation boundary for every threshold fact.  PostgreSQL
-- records the intended projection in the same ownership zone as the
-- regression truth; the ClickHouse writer may retry pending rows after a
-- process or provider failure without changing the immutable decision.
CREATE TABLE IF NOT EXISTS observatory_evaluation_regression_projection_outbox (
    projection_id UUID PRIMARY KEY,
    regression_id UUID NOT NULL,
    regression_version VARCHAR(64) NOT NULL,
    metric_id VARCHAR(128) NOT NULL,
    threshold_index SMALLINT NOT NULL DEFAULT 0,
    payload_json JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error VARCHAR(128),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied_at TIMESTAMPTZ,
    cleanup_pending BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (regression_id, regression_version, metric_id, threshold_index),
    CONSTRAINT observatory_regression_projection_status_ck CHECK (status IN ('pending', 'applied', 'failed', 'deleted')),
    CONSTRAINT observatory_regression_projection_capture_ck CHECK (payload_json ->> 'capturePolicy' = 'metadata_only')
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_regression_projection_pending_idx
    ON observatory_evaluation_regression_projection_outbox (status, created_at, projection_id);

CREATE TABLE IF NOT EXISTS observatory_evaluation_experiment_commands (
    idempotency_key VARCHAR(128) PRIMARY KEY,
    operation VARCHAR(32) NOT NULL,
    resource_id UUID,
    resource_version VARCHAR(64),
    request_fingerprint CHAR(64) NOT NULL,
    replay_json JSONB,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT observatory_experiment_command_fingerprint_ck CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT observatory_experiment_command_operation_ck CHECK (operation IN ('create_experiment', 'create_baseline', 'promote_baseline', 'create_regression'))
);

ALTER TABLE observatory_evaluation_experiment_commands
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS resource_id UUID,
    ADD COLUMN IF NOT EXISTS resource_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS lease_token UUID,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS history_reconciled_at TIMESTAMPTZ;
DROP INDEX IF EXISTS observatory_evaluation_experiment_commands_resource_idx;
CREATE INDEX IF NOT EXISTS observatory_evaluation_experiment_commands_resource_idx
    ON observatory_evaluation_experiment_commands (resource_id, resource_version, status, created_at DESC);

-- Retention fences pending projection writes before deleting their source
-- regression.  Tombstones are durable so a restarted reconciler cannot
-- resurrect facts selected by a deletion command.
ALTER TABLE observatory_evaluation_regression_projection_outbox /* IF NOT EXISTS */
    DROP CONSTRAINT IF EXISTS observatory_regression_projection_status_ck;
ALTER TABLE observatory_evaluation_regression_projection_outbox /* IF NOT EXISTS */
    ADD CONSTRAINT observatory_regression_projection_status_ck
    CHECK (status IN ('pending', 'applied', 'failed', 'deleted'));
ALTER TABLE observatory_evaluation_regression_projection_outbox
    ADD COLUMN IF NOT EXISTS cleanup_pending BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS observatory_evaluation_regression_projection_cleanup_idx
    ON observatory_evaluation_regression_projection_outbox (cleanup_pending, created_at, projection_id);

CREATE TABLE IF NOT EXISTS observatory_evaluation_experiment_history (
    history_id UUID PRIMARY KEY,
    resource_type VARCHAR(24) NOT NULL,
    resource_id VARCHAR(128) NOT NULL,
    resource_version VARCHAR(64),
    change VARCHAR(32) NOT NULL,
    state VARCHAR(32) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    capture_policy VARCHAR(16) NOT NULL DEFAULT 'metadata_only',
    CONSTRAINT observatory_experiment_history_capture_ck CHECK (capture_policy = 'metadata_only'),
    CONSTRAINT observatory_experiment_history_type_ck CHECK (resource_type IN ('experiment', 'baseline', 'regression', 'attribution'))
);
CREATE INDEX IF NOT EXISTS observatory_evaluation_experiment_history_page_idx
    ON observatory_evaluation_experiment_history (occurred_at DESC, history_id DESC);
