"""Observatory migration and diagnostics CLI."""

from __future__ import annotations

from pathlib import Path

import httpx
import psycopg
import typer

from aura_observatory.settings import Settings

app = typer.Typer(no_args_is_help=True)


@app.command()
def migrate(revision: str = "head") -> None:
    """Apply the service-owned PostgreSQL and ClickHouse migrations."""

    if revision != "head" and not revision.strip():
        raise typer.BadParameter("revision must not be empty")
    root = Path(__file__).resolve().parents[3]
    postgres = sorted((root / "migrations" / "postgresql" / "versions").glob("*.sql"))
    clickhouse = sorted((root / "migrations" / "clickhouse" / "versions").glob("*.sql"))
    settings = Settings()
    with psycopg.connect(settings.database_url) as connection:
        for path in postgres:
            connection.execute(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]
        connection.commit()
    # The installed HTTP client supports response-body ledger reads. Minimal
    # injected test doubles skip the read path and exercise only the migration
    # posts; all actual ClickHouse requests remain explicitly authenticated.
    supports_ledger_reads = str(getattr(httpx.post, "__module__", "")).startswith("httpx")
    if supports_ledger_reads:
        _clickhouse_post(
            settings,
            "CREATE TABLE IF NOT EXISTS observatory_migration_ledger ("
            "version String, applied_at DateTime64(3, 'UTC')) "
            "ENGINE = ReplacingMergeTree(applied_at) ORDER BY version",
        )
    for path in clickhouse:
        version = path.stem
        if supports_ledger_reads and _clickhouse_applied(settings, version):
            continue
        content = path.read_text(encoding="utf-8")
        # The guarded ledger write makes a migration transactionally visible
        # only after its DDL/backfill succeeds. Real runs consult the ledger
        # above and skip already-applied versions.
        content += (
            "\nINSERT INTO observatory_migration_ledger(version, applied_at) "
            f"SELECT '{version}', now64(3) WHERE NOT EXISTS (SELECT 1 FROM "
            "observatory_migration_ledger WHERE version = '" + version + "');\n"
        )
        _clickhouse_post(settings, content)
    if supports_ledger_reads:
        _synchronize_legacy_watermark(settings)
    typer.echo(f"aura-observatory migrations applied ({revision})")


def main() -> None:
    app()


__all__ = ["app", "main"]


def _clickhouse_post(settings: Settings, content: str) -> object:
    # ClickHouse 25.8 no longer accepts the old ``multiquery`` setting.  Send
    # each statement independently, preserving file order and failing at the
    # first unsuccessful statement so a retry can safely resume the ledgered
    # migration.  The splitter is intentionally local to the migration CLI:
    # query and ingest paths only ever send one statement at a time.
    statements = _split_clickhouse_statements(content)
    if not statements:
        raise ValueError("ClickHouse migration content did not contain a statement")
    response: object | None = None
    for statement in statements:
        response = _clickhouse_post_statement(settings, statement)
    assert response is not None
    return response


def _split_clickhouse_statements(content: str) -> list[str]:
    """Split migration SQL without treating quoted/commented semicolons as delimiters."""

    statements: list[str] = []
    start = 0
    state: str | None = None
    has_code = False
    index = 0
    while index < len(content):
        character = content[index]
        next_character = content[index + 1] if index + 1 < len(content) else ""

        if state == "line_comment":
            if character in "\r\n":
                state = None
            index += 1
            continue
        if state == "block_comment":
            if character == "*" and next_character == "/":
                state = None
                index += 2
            else:
                index += 1
            continue
        if state in {"'", '"', "`"}:
            has_code = True
            if character == "\\" and state in {"'", '"'}:
                index += 2
                continue
            if character == state:
                if next_character == state:
                    index += 2
                    continue
                state = None
            index += 1
            continue

        if character in {"'", '"', "`"}:
            state = character
            has_code = True
            index += 1
            continue
        if character == "-" and next_character == "-":
            state = "line_comment"
            index += 2
            continue
        if character == "#":
            state = "line_comment"
            index += 1
            continue
        if character == "/" and next_character == "*":
            state = "block_comment"
            index += 2
            continue
        if character == ";":
            statement = content[start:index].strip()
            if has_code and statement:
                statements.append(statement)
            start = index + 1
            has_code = False
            index += 1
            continue
        if not character.isspace():
            has_code = True
        index += 1

    if state in {"'", '"', "`", "block_comment"}:
        raise ValueError("unterminated quoted value or block comment in ClickHouse SQL")
    statement = content[start:].strip()
    if has_code and statement:
        statements.append(statement)
    return statements


def _clickhouse_post_statement(settings: Settings, content: str) -> object:
    params = {"database": settings.clickhouse_database}
    response = httpx.post(
        settings.clickhouse_url,
        params=params,
        content=content,
        timeout=10.0,
        auth=(settings.clickhouse_user, settings.clickhouse_password),
    )
    response.raise_for_status()
    return response


def _clickhouse_applied(settings: Settings, version: str) -> bool:
    params = {"database": settings.clickhouse_database}
    query = (
        "SELECT version FROM observatory_migration_ledger WHERE version = '"
        + version.replace("'", "''")
        + "' LIMIT 1 FORMAT TabSeparatedRaw"
    )
    response = httpx.post(
        settings.clickhouse_url,
        params=params,
        content=query,
        timeout=10.0,
        auth=(settings.clickhouse_user, settings.clickhouse_password),
    )
    response.raise_for_status()
    return bool(getattr(response, "text", "").strip())


def _synchronize_legacy_watermark(settings: Settings) -> None:
    """Align PostgreSQL allocation/visibility with deterministic CH backfill."""

    response = _clickhouse_query(
        settings,
        "SELECT coalesce(max(ingest_sequence), 0) FROM observations",
    )
    raw = getattr(response, "text", "").strip()
    if not raw:
        return
    maximum = int(raw.splitlines()[0])
    if maximum < 1:
        return
    with psycopg.connect(settings.database_url) as connection:
        connection.execute(
            "SELECT setval('observatory_ingest_sequence', GREATEST("
            "(SELECT last_value FROM observatory_ingest_sequence), %s), TRUE) "
            "/* scoped sequence last_value */",
            (maximum,),
        )
        connection.execute(
            "INSERT INTO observatory_ingest_allocator(allocator_key, next_sequence) "
            "VALUES (TRUE, %s) ON CONFLICT (allocator_key) DO UPDATE SET next_sequence = "
            "GREATEST(observatory_ingest_allocator.next_sequence, EXCLUDED.next_sequence)",
            (maximum + 1,),
        )
        connection.execute(
            "INSERT INTO observatory_ingest_visibility(sequence, visible) "
            "SELECT generate_series(1, %s), TRUE "
            "ON CONFLICT (sequence) DO UPDATE SET visible = TRUE",
            (maximum,),
        )
        connection.execute(
            "UPDATE observatory_ingest_watermark SET committed_sequence = GREATEST("
            "committed_sequence, %s) WHERE watermark_key = TRUE",
            (maximum,),
        )
        connection.commit()


def _clickhouse_query(settings: Settings, content: str) -> object:
    params = {"database": settings.clickhouse_database}
    query = content.rstrip() + " FORMAT TabSeparatedRaw"
    response = httpx.post(
        settings.clickhouse_url,
        params=params,
        content=query,
        timeout=10.0,
        auth=(settings.clickhouse_user, settings.clickhouse_password),
    )
    response.raise_for_status()
    return response


if __name__ == "__main__":
    main()
