-- AURA-0097 durable dashboards, bounded alerting, incidents and annotations.
-- Observatory-owned JSON is written only after application closed-schema
-- validation. No telemetry or Core table is referenced by this migration.

CREATE TABLE IF NOT EXISTS observatory_operations_commands (
    command_id VARCHAR(128) PRIMARY KEY,
    replay_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS observatory_operations_revision_seq;

CREATE TABLE IF NOT EXISTS observatory_dashboards (
    resource_id UUID NOT NULL,
    resource_version VARCHAR(64) NOT NULL,
    state VARCHAR(32), kind VARCHAR(64), severity VARCHAR(32),
    resource_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (resource_id, resource_version)
);
DO $$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'observatory_dashboards', 'observatory_saved_queries', 'observatory_alert_rules',
        'observatory_alert_instances', 'observatory_alert_suppressions',
        'observatory_incidents', 'observatory_annotations',
        'observatory_alert_acknowledgements'
    ] LOOP
        EXECUTE format($sql$
            CREATE TABLE IF NOT EXISTS %I (
                resource_id UUID NOT NULL,
                resource_version VARCHAR(64) NOT NULL,
                state VARCHAR(32), kind VARCHAR(64), severity VARCHAR(32),
                resource_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (resource_id, resource_version)
            )$sql$, table_name);
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS observatory_operations_created_idx
    ON observatory_dashboards (created_at, resource_id, resource_version);
CREATE INDEX IF NOT EXISTS observatory_saved_queries_kind_idx
    ON observatory_saved_queries (kind, created_at, resource_id);
CREATE INDEX IF NOT EXISTS observatory_alert_rule_state_idx
    ON observatory_alert_rules (state, created_at, resource_id);
CREATE INDEX IF NOT EXISTS observatory_alert_instance_state_idx
    ON observatory_alert_instances (state, severity, created_at, resource_id);
CREATE INDEX IF NOT EXISTS observatory_incident_state_idx
    ON observatory_incidents (state, severity, created_at, resource_id);

DO $$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'observatory_dashboards', 'observatory_saved_queries',
        'observatory_alert_rules', 'observatory_alert_instances',
        'observatory_alert_suppressions', 'observatory_alert_acknowledgements',
        'observatory_incidents', 'observatory_annotations'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS current_version VARCHAR(64)', table_name);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS visibility_revision BIGINT', table_name);
        EXECUTE format('UPDATE %I SET current_version = resource_version WHERE current_version IS NULL', table_name);
        EXECUTE format('UPDATE %I SET visibility_revision = nextval(''observatory_operations_revision_seq'') WHERE visibility_revision IS NULL', table_name);
    END LOOP;
END $$;

ALTER TABLE observatory_alert_rules ADD COLUMN IF NOT EXISTS next_due_at TIMESTAMPTZ;
ALTER TABLE observatory_alert_rules ADD COLUMN IF NOT EXISTS lease_token UUID;
ALTER TABLE observatory_alert_rules ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(128);
ALTER TABLE observatory_alert_rules ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE observatory_alert_rules ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE observatory_alert_rules ADD COLUMN IF NOT EXISTS last_failure_code VARCHAR(64);
CREATE INDEX IF NOT EXISTS observatory_alert_rule_due_idx
    ON observatory_alert_rules (state, next_due_at, lease_expires_at, resource_id);

ALTER TABLE observatory_operations_commands ADD COLUMN IF NOT EXISTS operation VARCHAR(64);
ALTER TABLE observatory_operations_commands ADD COLUMN IF NOT EXISTS target_key VARCHAR(256);
ALTER TABLE observatory_operations_commands ADD COLUMN IF NOT EXISTS request_hash VARCHAR(128);
ALTER TABLE observatory_operations_commands ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'completed';
CREATE UNIQUE INDEX IF NOT EXISTS observatory_operations_command_scope_idx
    ON observatory_operations_commands (operation, target_key, request_hash);

CREATE TABLE IF NOT EXISTS observatory_operations_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    resource_type VARCHAR(64) NOT NULL,
    resource_id UUID NOT NULL,
    resource_version VARCHAR(64) NOT NULL,
    change VARCHAR(32) NOT NULL,
    state VARCHAR(32),
    event_json JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS observatory_operations_events_cursor_idx
    ON observatory_operations_events (sequence, event_id);

CREATE TABLE IF NOT EXISTS observatory_operations_audit (
    audit_sequence BIGSERIAL,
    audit_id UUID NOT NULL,
    audit_version VARCHAR(64) NOT NULL,
    operation VARCHAR(64) NOT NULL,
    resource_type VARCHAR(64) NOT NULL,
    resource_id UUID,
    resource_version VARCHAR(64),
    outcome VARCHAR(32) NOT NULL,
    state VARCHAR(64),
    occurred_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (audit_id, audit_version),
    UNIQUE (audit_sequence)
);
ALTER TABLE observatory_operations_audit ADD COLUMN IF NOT EXISTS audit_sequence BIGSERIAL;
ALTER TABLE observatory_operations_audit ADD COLUMN IF NOT EXISTS state VARCHAR(64);
ALTER TABLE observatory_operations_audit ADD COLUMN IF NOT EXISTS mutation_revision BIGINT;
UPDATE observatory_operations_audit
SET mutation_revision = nextval('observatory_operations_revision_seq')
WHERE mutation_revision IS NULL;
CREATE INDEX IF NOT EXISTS observatory_operations_audit_cursor_idx
    ON observatory_operations_audit (occurred_at, audit_id, audit_sequence);
CREATE INDEX IF NOT EXISTS observatory_operations_audit_mutation_idx
    ON observatory_operations_audit (mutation_revision, resource_type, resource_id, operation);

INSERT INTO observatory_migration_ledger(version, applied_at)
SELECT '0014_observatory_operations', CURRENT_TIMESTAMP
WHERE NOT EXISTS (
    SELECT 1 FROM observatory_migration_ledger
    WHERE version = '0014_observatory_operations'
);
