-- Versioned Observatory-owned component metadata.  Definitions and edges are
-- deliberately separate from high-cardinality ClickHouse observations.  This
-- migration has no retention or automatic TTL policy: owner retention removes
-- observations while preserving catalogue definitions and dependency history.
CREATE TABLE IF NOT EXISTS observatory_component_definitions (
    component_id VARCHAR(96) NOT NULL,
    component_version VARCHAR(128) NOT NULL,
    owner VARCHAR(128) NOT NULL,
    category VARCHAR(128) NOT NULL,
    input_schema_id VARCHAR(128) NOT NULL,
    output_schema_id VARCHAR(128) NOT NULL,
    measurements VARCHAR[] NOT NULL DEFAULT '{}',
    evaluators VARCHAR[] NOT NULL DEFAULT '{}',
    manifest_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (component_id, component_version)
);

CREATE TABLE IF NOT EXISTS observatory_component_dependencies (
    dependency_edge_id BIGSERIAL PRIMARY KEY,
    source_component_id VARCHAR(96) NOT NULL,
    source_component_version VARCHAR(128) NOT NULL,
    dependency_id VARCHAR(128) NOT NULL,
    dependency_kind VARCHAR(16) NOT NULL,
    required_version VARCHAR(128),
    CONSTRAINT observatory_component_dependencies_source_fk
        FOREIGN KEY (source_component_id, source_component_version)
        REFERENCES observatory_component_definitions(component_id, component_version)
        ON DELETE RESTRICT
);

-- Upgrade installations created from the pre-release draft where the
-- nullable required_version was incorrectly part of the primary key.
ALTER TABLE observatory_component_dependencies
    ADD COLUMN IF NOT EXISTS dependency_edge_id BIGSERIAL;
ALTER TABLE observatory_component_dependencies
    DROP CONSTRAINT IF EXISTS observatory_component_dependencies_pkey
    /* IF NOT EXISTS: the legacy constraint is absent on a fresh install */;
ALTER TABLE observatory_component_dependencies
    ALTER COLUMN required_version DROP NOT NULL
    /* IF NOT EXISTS: nullable is already the fresh-install shape */;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'observatory_component_dependencies'::regclass
          AND conname = 'observatory_component_dependencies_edge_pkey'
    ) THEN
        ALTER TABLE observatory_component_dependencies /* IF NOT EXISTS */
            ADD CONSTRAINT observatory_component_dependencies_edge_pkey
            PRIMARY KEY (dependency_edge_id);
    END IF;
END $$;

DELETE FROM observatory_component_dependencies older
USING observatory_component_dependencies newer
WHERE older.dependency_edge_id > newer.dependency_edge_id
  AND older.source_component_id = newer.source_component_id
  AND older.source_component_version = newer.source_component_version
  AND older.dependency_id = newer.dependency_id
  AND older.dependency_kind = newer.dependency_kind
  AND older.required_version IS NOT DISTINCT FROM newer.required_version;

CREATE UNIQUE INDEX IF NOT EXISTS observatory_component_dependencies_identity_idx
    ON observatory_component_dependencies(
        source_component_id, source_component_version, dependency_id,
        dependency_kind, COALESCE(required_version, '')
    );

CREATE INDEX IF NOT EXISTS observatory_component_dependencies_target_idx
    ON observatory_component_dependencies(dependency_id, required_version);
