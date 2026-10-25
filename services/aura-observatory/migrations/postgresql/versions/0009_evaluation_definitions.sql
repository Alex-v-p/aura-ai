-- AURA-0093 immutable definition state.
-- These tables contain only bounded identifiers, digests, typed expectations,
-- and opaque artifact references.  Authorized bytes stay behind the existing
-- private object-store boundary and are never copied into definition state.

CREATE TABLE IF NOT EXISTS observatory_evaluation_datasets (
    dataset_id UUID NOT NULL,
    dataset_version VARCHAR(64) NOT NULL,
    source_kind VARCHAR(16) NOT NULL,
    scopes VARCHAR[] NOT NULL,
    capture_classification VARCHAR(16) NOT NULL,
    authorization_purpose VARCHAR(32) NOT NULL,
    authorization_record_id UUID,
    authorized_at TIMESTAMPTZ,
    source_digest CHAR(64) NOT NULL,
    created_by_component VARCHAR(96) NOT NULL,
    created_by_version VARCHAR(64) NOT NULL,
    retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    case_ids UUID[] NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    definition_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (dataset_id, dataset_version),
    CONSTRAINT observatory_eval_dataset_source_ck CHECK (source_kind IN ('synthetic', 'owner_supplied', 'captured')),
    CONSTRAINT observatory_eval_dataset_capture_ck CHECK (capture_classification IN ('metadata_only', 'hashed', 'redacted', 'sampled_content', 'full_content')),
    CONSTRAINT observatory_eval_dataset_retention_ck CHECK (retention_mode = 'manual_only'),
    CONSTRAINT observatory_eval_dataset_fingerprint_ck CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT observatory_eval_dataset_digest_ck CHECK (source_digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS observatory_evaluation_cases (
    case_id UUID NOT NULL,
    case_version VARCHAR(64) NOT NULL,
    dataset_id UUID NOT NULL,
    dataset_version VARCHAR(64) NOT NULL,
    source_kind VARCHAR(16) NOT NULL,
    scopes VARCHAR[] NOT NULL,
    capture_classification VARCHAR(16) NOT NULL,
    authorization_purpose VARCHAR(32) NOT NULL,
    authorization_record_id UUID,
    authorized_at TIMESTAMPTZ,
    content_digest CHAR(64),
    artifact_reference_ids UUID[] NOT NULL DEFAULT '{}',
    expectations_json JSONB,
    source_digest CHAR(64) NOT NULL,
    created_by_component VARCHAR(96) NOT NULL,
    created_by_version VARCHAR(64) NOT NULL,
    retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    fingerprint CHAR(64) NOT NULL,
    definition_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (case_id, case_version),
    CONSTRAINT observatory_eval_case_source_ck CHECK (source_kind IN ('synthetic', 'owner_supplied', 'captured')),
    CONSTRAINT observatory_eval_case_capture_ck CHECK (capture_classification IN ('metadata_only', 'hashed', 'redacted', 'sampled_content', 'full_content')),
    CONSTRAINT observatory_eval_case_retention_ck CHECK (retention_mode = 'manual_only'),
    CONSTRAINT observatory_eval_case_fingerprint_ck CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT observatory_eval_case_digest_ck CHECK (content_digest IS NULL OR content_digest ~ '^[0-9a-f]{64}$'),
    FOREIGN KEY (dataset_id, dataset_version)
        REFERENCES observatory_evaluation_datasets(dataset_id, dataset_version)
        ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS observatory_eval_cases_dataset_idx
    ON observatory_evaluation_cases(dataset_id, dataset_version, case_id, case_version);

CREATE TABLE IF NOT EXISTS observatory_evaluation_dataset_cases (
    dataset_id UUID NOT NULL,
    dataset_version VARCHAR(64) NOT NULL,
    case_id UUID NOT NULL,
    case_version VARCHAR(64) NOT NULL,
    PRIMARY KEY (dataset_id, dataset_version, case_id),
    FOREIGN KEY (dataset_id, dataset_version)
        REFERENCES observatory_evaluation_datasets(dataset_id, dataset_version)
        ON DELETE RESTRICT,
    FOREIGN KEY (case_id, case_version)
        REFERENCES observatory_evaluation_cases(case_id, case_version)
        ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS observatory_eval_dataset_cases_case_idx
    ON observatory_evaluation_dataset_cases(case_id, case_version);

CREATE TABLE IF NOT EXISTS observatory_evaluation_evaluators (
    evaluator_id UUID NOT NULL,
    evaluator_version VARCHAR(64) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    metric VARCHAR(128) NOT NULL,
    component_ids VARCHAR[] NOT NULL,
    source_digest CHAR(64) NOT NULL,
    created_by_component VARCHAR(96) NOT NULL,
    created_by_version VARCHAR(64) NOT NULL,
    retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    fingerprint CHAR(64) NOT NULL,
    definition_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (evaluator_id, evaluator_version),
    CONSTRAINT observatory_eval_evaluator_kind_ck CHECK (kind = 'deterministic'),
    CONSTRAINT observatory_eval_evaluator_retention_ck CHECK (retention_mode = 'manual_only'),
    CONSTRAINT observatory_eval_evaluator_fingerprint_ck CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT observatory_eval_evaluator_digest_ck CHECK (source_digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS observatory_evaluation_suites (
    suite_id UUID NOT NULL,
    suite_version VARCHAR(64) NOT NULL,
    dataset_id UUID NOT NULL,
    dataset_version VARCHAR(64) NOT NULL,
    source_digest CHAR(64) NOT NULL,
    created_by_component VARCHAR(96) NOT NULL,
    created_by_version VARCHAR(64) NOT NULL,
    timeout_ms INTEGER NOT NULL,
    max_concurrency SMALLINT NOT NULL,
    max_attempts SMALLINT NOT NULL,
    max_cases INTEGER NOT NULL,
    max_input_bytes BIGINT NOT NULL,
    max_output_bytes BIGINT NOT NULL,
    max_tokens BIGINT NOT NULL,
    retention_mode VARCHAR(16) NOT NULL DEFAULT 'manual_only',
    fingerprint CHAR(64) NOT NULL,
    definition_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (suite_id, suite_version),
    CONSTRAINT observatory_eval_suite_limits_ck CHECK (timeout_ms BETWEEN 100 AND 86400000 AND max_concurrency BETWEEN 1 AND 64 AND max_attempts BETWEEN 1 AND 10 AND max_cases BETWEEN 1 AND 100000 AND max_input_bytes BETWEEN 0 AND 1073741824 AND max_output_bytes BETWEEN 0 AND 1073741824 AND max_tokens BETWEEN 0 AND 100000000),
    CONSTRAINT observatory_eval_suite_retention_ck CHECK (retention_mode = 'manual_only'),
    CONSTRAINT observatory_eval_suite_fingerprint_ck CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT observatory_eval_suite_digest_ck CHECK (source_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT observatory_eval_suite_dataset_fk
        FOREIGN KEY (dataset_id, dataset_version)
        REFERENCES observatory_evaluation_datasets(dataset_id, dataset_version)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS observatory_evaluation_suite_evaluators (
    suite_id UUID NOT NULL,
    suite_version VARCHAR(64) NOT NULL,
    evaluator_id UUID NOT NULL,
    evaluator_version VARCHAR(64) NOT NULL,
    ordinal SMALLINT NOT NULL,
    PRIMARY KEY (suite_id, suite_version, evaluator_id, evaluator_version),
    FOREIGN KEY (suite_id, suite_version) REFERENCES observatory_evaluation_suites(suite_id, suite_version) ON DELETE RESTRICT,
    FOREIGN KEY (evaluator_id, evaluator_version) REFERENCES observatory_evaluation_evaluators(evaluator_id, evaluator_version) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS observatory_evaluation_suite_components (
    suite_id UUID NOT NULL,
    suite_version VARCHAR(64) NOT NULL,
    component_id VARCHAR(96) NOT NULL,
    component_version VARCHAR(64) NOT NULL,
    code_revision VARCHAR(128) NOT NULL,
    image_digest CHAR(64) NOT NULL,
    configuration_revision VARCHAR(128) NOT NULL,
    ordinal SMALLINT NOT NULL,
    PRIMARY KEY (suite_id, suite_version, component_id, component_version),
    FOREIGN KEY (suite_id, suite_version) REFERENCES observatory_evaluation_suites(suite_id, suite_version) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS observatory_evaluation_suite_thresholds (
    suite_id UUID NOT NULL,
    suite_version VARCHAR(64) NOT NULL,
    evaluator_id UUID NOT NULL,
    evaluator_version VARCHAR(64) NOT NULL,
    metric VARCHAR(128) NOT NULL,
    aggregation VARCHAR(16) NOT NULL,
    operator VARCHAR(24) NOT NULL,
    threshold_value DOUBLE PRECISION NOT NULL,
    unit VARCHAR(16) NOT NULL,
    minimum_sample_size INTEGER NOT NULL,
    missing_evidence_policy VARCHAR(16) NOT NULL,
    PRIMARY KEY (suite_id, suite_version, evaluator_id, evaluator_version, metric),
    FOREIGN KEY (suite_id, suite_version) REFERENCES observatory_evaluation_suites(suite_id, suite_version) ON DELETE RESTRICT,
    FOREIGN KEY (evaluator_id, evaluator_version) REFERENCES observatory_evaluation_evaluators(evaluator_id, evaluator_version) ON DELETE RESTRICT,
    CONSTRAINT observatory_eval_threshold_value_ck CHECK (threshold_value = threshold_value AND threshold_value BETWEEN -1000000000000000 AND 1000000000000000),
    CONSTRAINT observatory_eval_threshold_sample_ck CHECK (minimum_sample_size BETWEEN 1 AND 100000)
);

CREATE TABLE IF NOT EXISTS observatory_evaluation_definition_audits (
    audit_id UUID PRIMARY KEY,
    resource_type VARCHAR(16) NOT NULL,
    resource_id UUID NOT NULL,
    resource_version VARCHAR(64) NOT NULL,
    operation VARCHAR(16) NOT NULL,
    outcome VARCHAR(24) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT observatory_eval_definition_audit_type_ck CHECK (resource_type IN ('dataset', 'case', 'suite', 'evaluator')),
    CONSTRAINT observatory_eval_definition_audit_operation_ck CHECK (operation IN ('create', 'idempotent_reuse', 'conflict', 'reject')),
    CONSTRAINT observatory_eval_definition_audit_outcome_ck CHECK (outcome IN ('accepted', 'duplicate', 'rejected'))
);
CREATE INDEX IF NOT EXISTS observatory_eval_definition_audits_resource_idx
    ON observatory_evaluation_definition_audits(resource_type, resource_id, resource_version, occurred_at);
