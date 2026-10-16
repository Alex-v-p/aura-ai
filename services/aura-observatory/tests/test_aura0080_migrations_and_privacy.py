"""AURA-0080 schema-shape and retention-safety regression checks."""

from __future__ import annotations

import ast
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aura_observatory.adapters.postgres import PostgresObservationIdRegistry

SERVICE_ROOT = Path(__file__).parents[1]
SRC_ROOT = SERVICE_ROOT / "src" / "aura_observatory"


def test_observatory_query_source_stays_independent_and_content_free() -> None:
    for path in SRC_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(
                not name == "aura_core" and not name.startswith("aura_core.") for name in names
            )
        if path.name == "query.py":
            lowered = source.lower()
            assert '"prompt"' not in lowered
            assert '"response"' not in lowered
            assert '"bearertoken"' not in lowered
            assert '"owner_identity"' not in lowered


@pytest.mark.parametrize(
    "migration_dir",
    [SERVICE_ROOT / "migrations" / "postgresql", SERVICE_ROOT / "migrations" / "clickhouse"],
)
def test_migrations_are_rerunnable_and_do_not_enable_automatic_ttl(migration_dir: Path) -> None:
    migrations = sorted(migration_dir.rglob("*.sql"))
    assert migrations, f"missing migrations under {migration_dir}"
    text = "\n".join(path.read_text(encoding="utf-8").lower() for path in migrations)
    assert "create table if not exists" in text
    if migration_dir.name == "clickhouse":
        assert "drop table if exists observatory_legacy_ordering" in text
        assert "drop table if exists observatory_legacy_ordering;" in text
    else:
        assert "drop table" not in text
    # Comments may explain that automatic TTL is intentionally absent; a
    # ClickHouse TTL clause must never become executable retention behavior.
    executable_lines = [line for line in text.splitlines() if not line.lstrip().startswith("--")]
    assert not any(" ttl " in f" {line} " for line in executable_lines)


def test_postgres_schema_has_retention_state_and_content_free_audit_shape() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (SERVICE_ROOT / "migrations" / "postgresql").rglob("*.sql")
    )
    assert "retention" in text
    assert "audit" in text
    for forbidden in ("prompt", "response", "filename", "credential", "bearer", "owner_identity"):
        assert forbidden not in text


def test_clickhouse_schema_keeps_observations_and_span_topology_service_owned() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (SERVICE_ROOT / "migrations" / "clickhouse").rglob("*.sql")
    )
    assert "observation" in text
    assert "span" in text
    assert "run_id" in text
    assert "parent_span_id" in text
    assert "run_projections" in text
    assert "non_replicated_deduplication_window" in text


@pytest.mark.asyncio
async def test_receipt_observed_at_is_written_for_cutoff_deletion() -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class Connection:
        async def execute(self, statement: str, params: tuple[Any, ...] = ()) -> None:
            calls.append((statement, params))

    @asynccontextmanager
    async def connection() -> AsyncGenerator[Connection]:
        yield Connection()

    registry = PostgresObservationIdRegistry(connection)
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    await registry.set_observed_at("01234567-89ab-4cde-8123-0123456789ab", observed_at)

    assert len(calls) == 1
    statement, params = calls[0]
    assert "observed_at" in statement
    assert params == (observed_at, "01234567-89ab-4cde-8123-0123456789ab")


def test_legacy_null_receipts_and_projection_cleanup_are_explicitly_guarded() -> None:
    query_source = "\n".join(
        (
            (SRC_ROOT / "query.py").read_text(encoding="utf-8"),
            (SRC_ROOT / "adapters" / "query.py").read_text(encoding="utf-8"),
        )
    ).lower()
    assert "if cutoff is none" in query_source
    assert "or observation_id = any" in query_source
    assert "alter table run_projections delete" in query_source
    postgres_source = (SRC_ROOT / "adapters" / "postgres.py").read_text(encoding="utf-8").lower()
    assert "set observed_at" in postgres_source
