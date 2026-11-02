"""AURA-0086 catalogue synchronization and bounded topology regressions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from aura_observatory.adapters.postgres import PostgresSyncCatalogueAdapter
from aura_observatory.entrypoints.cli import load_component_manifests

SERVICE_ROOT = Path(__file__).parents[1]


def test_packaged_manifest_loader_is_closed_and_deterministic() -> None:
    definitions = load_component_manifests()
    assert [
        (item["componentId"], item["componentVersion"]) for item in definitions
    ] == sorted((item["componentId"], item["componentVersion"]) for item in definitions)
    assert definitions
    for definition in definitions:
        assert set(definition) == {
            "componentId",
            "componentVersion",
            "owner",
            "category",
            "inputSchemaId",
            "outputSchemaId",
            "measurements",
            "evaluators",
            "dependencies",
        }
        assert all("prompt" not in str(value).lower() for value in definition.values())


def test_manifest_loader_rejects_duplicates_and_cycles(tmp_path: Path) -> None:
    manifest: dict[str, Any] = {
        "component": {
            "id": "aura.test.first",
            "version": "1.0.0",
            "owner": "test.owner",
            "category": "test",
        },
        "input": {"schema": "test-input-v1"},
        "output": {"schema": "test-output-v1"},
        "dependencies": ["aura.test.second"],
        "metrics": [],
        "evaluators": [],
    }
    (tmp_path / "first.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        (tmp_path / "duplicate.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        load_component_manifests(tmp_path)

    second = dict(manifest)
    second["component"] = {**manifest["component"], "id": "aura.test.second"}
    second["dependencies"] = ["aura.test.first"]
    (tmp_path / "duplicate.yaml").write_text(yaml.safe_dump(second), encoding="utf-8")
    with pytest.raises(ValueError, match="cycle"):
        load_component_manifests(tmp_path)


def test_component_migration_is_forward_safe_and_preserves_catalogue() -> None:
    migration = (
        SERVICE_ROOT / "migrations/postgresql/versions/0006_component_definitions.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create table if not exists observatory_component_definitions" in migration
    assert "create table if not exists observatory_component_dependencies" in migration
    assert "on delete restrict" in migration
    assert "ttl" not in "\n".join(
        line for line in migration.splitlines() if not line.lstrip().startswith("--")
    )
    assert "required_version varchar(128)" in migration
    assert "coalesce(required_version, '')" in migration
    assert "drop constraint if exists observatory_component_dependencies_pkey" in migration


def test_sync_adapter_uses_parameterized_idempotent_statements() -> None:
    class Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
            self.calls.append((statement, parameters))

    connection = Connection()
    definitions = load_component_manifests()
    count = PostgresSyncCatalogueAdapter(connection).sync_component_definitions(definitions)

    assert count == len(definitions)
    assert connection.calls
    assert all(
        "%s" in statement or "CURRENT_TIMESTAMP" in statement
        for statement, _ in connection.calls
    )
    assert all("prompt" not in repr(parameters).lower() for _, parameters in connection.calls)


def test_migration_container_entrypoint_keeps_manifest_sync_in_no_argument_path() -> None:
    source = (SERVICE_ROOT / "src/aura_observatory/entrypoints/cli.py").read_text(
        encoding="utf-8"
    )
    assert 'def migrate(revision: str = "head")' in source
    assert "PostgresSyncCatalogueAdapter(connection).sync_component_definitions" in source
