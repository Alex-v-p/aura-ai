"""Deterministic AURA-0100 fresh-schema migration startup regressions."""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION_ROOT = Path(__file__).parents[1] / "migrations/postgresql/versions"


def _postgres_migrations() -> list[Path]:
    return sorted(MIGRATION_ROOT.glob("*.sql"))


def _without_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _ledger_insert_versions(sql: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", _without_comments(sql)).strip()
    return re.findall(
        r"INSERT INTO observatory_migration_ledger\s*\(version, applied_at\)"
        r"\s*SELECT\s+'([^']+)'\s*,\s*CURRENT_TIMESTAMP\s*"
        r"WHERE NOT EXISTS\s*\(\s*SELECT 1 FROM observatory_migration_ledger\s*"
        r"WHERE version = '([^']+)'\s*\)",
        normalized,
        flags=re.IGNORECASE,
    )


def test_postgresql_ledger_is_created_before_any_ledger_insert() -> None:
    migrations = _postgres_migrations()
    assert migrations

    ledger_creation = "CREATE TABLE IF NOT EXISTS observatory_migration_ledger"
    creation_positions = [
        (
            migration.name,
            _without_comments(migration.read_text(encoding="utf-8")).find(ledger_creation),
        )
        for migration in migrations
    ]
    creation = [(name, position) for name, position in creation_positions if position >= 0]
    assert [name for name, _position in creation] == ["0001_observatory_foundation.sql"]

    ledger_insert_files = [
        migration
        for migration in migrations
        if "INSERT INTO observatory_migration_ledger" in _without_comments(
            migration.read_text(encoding="utf-8")
        )
    ]
    assert [migration.name for migration in ledger_insert_files] == [
        "0013_evaluation_experiment_live_events.sql",
        "0014_observatory_operations.sql",
    ]
    assert migrations.index(ledger_insert_files[0]) > migrations.index(
        Path(MIGRATION_ROOT / "0001_observatory_foundation.sql")
    )


def test_postgresql_0013_and_0014_are_the_only_guarded_ledger_records() -> None:
    expected = {
        "0013_evaluation_experiment_live_events.sql": "0013_evaluation_experiment_live_events",
        "0014_observatory_operations.sql": "0014_observatory_operations",
    }
    records: dict[str, tuple[str, str]] = {}
    for migration in _postgres_migrations():
        matches = _ledger_insert_versions(migration.read_text(encoding="utf-8"))
        if matches:
            assert migration.name in expected
            assert len(matches) == 1
            selected, guarded = matches[0]
            records[migration.name] = (selected, guarded)
        elif "observatory_migration_ledger" in migration.read_text(encoding="utf-8"):
            assert "INSERT INTO observatory_migration_ledger" not in migration.read_text(
                encoding="utf-8"
            )

    assert records == {name: (version, version) for name, version in expected.items()}
