"""Forward-only migration checks for durable notification delivery."""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0015_notification_delivery.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_notification_migration_is_the_non_destructive_successor() -> None:
    source = _source()
    assert re.search(
        r'^revision\s*=\s*["\']0015_notification_delivery["\']', source, re.MULTILINE
    )
    assert re.search(
        r'^down_revision\s*=\s*["\']0014_reminder_foundation["\']', source, re.MULTILINE
    )
    assert "downgrade is disabled" in source.lower()
    assert "DROP TABLE" not in source.upper()
    assert "TRUNCATE" not in source.upper()
    assert "DELETE FROM" not in source.upper()


def test_notification_tables_have_owner_scope_lifecycle_and_exactly_once_shape() -> None:
    source = _source()
    for table in ("notifications", "notification_events"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in source
    for column in (
        "principal_issuer",
        "principal_subject",
        "reminder_id",
        "status",
        "version",
        "created_at",
        "updated_at",
        "delivered_at",
        "acknowledged_at",
    ):
        assert column in source
    assert "UNIQUE (reminder_id)" in source or "UNIQUE (reminder_id," in source
    assert "status IN ('delivered', 'acknowledged')" in source
    assert "FOR UPDATE SKIP LOCKED" not in source


def test_notification_indexes_support_owner_replay_and_unread_queries() -> None:
    source = _source()
    assert "notification_events" in source
    assert "principal_issuer, principal_subject" in source
    assert "created_at" in source
    assert "acknowledged" in source
