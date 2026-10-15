"""AURA-0078 architecture, migration, and persistence-shape checks."""

from __future__ import annotations

import ast
from pathlib import Path

SERVICE_ROOT = Path(__file__).parents[1]
SRC_ROOT = SERVICE_ROOT / "src" / "aura_observatory"


def _python_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def test_observatory_source_has_no_core_import_or_database_reference() -> None:
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            assert all(
                not name == "aura_core" and not name.startswith("aura_core.") for name in imported
            ), f"{path} imports Core implementation"

        source = path.read_text(encoding="utf-8").lower()
        assert "aura_core" not in source
        assert "core_database" not in source


def test_observatory_has_service_owned_postgres_and_clickhouse_migrations() -> None:
    postgres = sorted((SERVICE_ROOT / "migrations" / "postgresql").rglob("*.sql"))
    clickhouse = sorted((SERVICE_ROOT / "migrations" / "clickhouse").rglob("*.sql"))

    assert postgres, "Observatory PostgreSQL migration chain is missing"
    assert clickhouse, "Observatory ClickHouse migration chain is missing"

    postgres_text = "\n".join(path.read_text(encoding="utf-8").lower() for path in postgres)
    clickhouse_text = "\n".join(path.read_text(encoding="utf-8").lower() for path in clickhouse)
    assert "component" in postgres_text
    assert "deployment" in postgres_text
    assert "ingest" in postgres_text
    assert "create table if not exists" in postgres_text
    assert "drop table" not in postgres_text
    assert "observation" in clickhouse_text
    assert "span" in clickhouse_text
    assert "create table if not exists" in clickhouse_text
    assert "replacingmergetree" in clickhouse_text
    assert "non_replicated_deduplication_window" in clickhouse_text
    # The migration comment may document that TTL is intentionally absent; a
    # ClickHouse TTL clause is the production behavior that must remain absent.
    assert " ttl " not in f" {clickhouse_text.replace('no ttl clause', '')} ", (
        "AURA-0078 must not add automatic TTL"
    )


def test_persistence_contracts_are_narrow_and_do_not_expose_orm_tables() -> None:
    ports = SRC_ROOT / "ports.py"
    source = ports.read_text(encoding="utf-8").lower()

    assert "protocol" in source
    assert "insert_observation" in source
    assert "increment_counter" in source
    assert "sqlalchemy" not in source
    assert "psycopg" not in source
    assert "clickhouse_driver" not in source
