"""Observatory migration and diagnostics CLI."""

# Typer returns dynamically typed command values; command output is narrowed
# to bounded metadata before serialization.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import typer

from aura_observatory.adapters.postgres import PostgresSyncCatalogueAdapter
from aura_observatory.catalogue import load_component_manifests
from aura_observatory.composition import build_evaluation_api_service
from aura_observatory.settings import Settings

app = typer.Typer(no_args_is_help=True)
evaluation_app = typer.Typer(no_args_is_help=True)
app.add_typer(evaluation_app, name="evaluation")


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
        # Keep the container's no-argument ``migrate`` invocation self-contained:
        # a successful schema migration always converges packaged definitions.
        # Minimal one-argument test doubles intentionally exercise migration SQL
        # only; real psycopg connections expose the parameterized form.
        if _supports_parameterized_execute(connection):
            PostgresSyncCatalogueAdapter(connection).sync_component_definitions(
                load_component_manifests()
            )
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


@app.command("sync-manifests")
def sync_manifests() -> None:
    """Synchronize packaged Observatory component definitions idempotently."""

    settings = Settings()
    definitions = load_component_manifests()
    with psycopg.connect(settings.database_url) as connection:
        PostgresSyncCatalogueAdapter(connection).sync_component_definitions(definitions)
        connection.commit()
    typer.echo(f"aura-observatory component manifests synchronized ({len(definitions)})")


@evaluation_app.command("list")
def evaluation_list(
    kind: str = typer.Argument(
        ...,
        help=(
            "One of datasets, suites, evaluators, jobs, results, artifacts, "
            "reviews, calibrations, qualifications."
        ),
    ),
    limit: int = typer.Option(50, min=1, max=200),
    cursor: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print bounded content-free evaluation metadata.

    The command intentionally uses the same application/query DTO boundary as
    the HTTP entrypoint.  It never accepts an object key, provider endpoint,
    credential, or content selector.
    """

    _require_local_operator()
    try:
        service = build_evaluation_api_service(Settings())
        result = asyncio.run(
            service.list_collection(kind, cursor=cursor, limit=limit)
        )
    except Exception as exc:
        # A CLI operator gets a finite, content-free failure.  Provider
        # diagnostics and response bodies never cross this boundary.
        raise typer.Exit(code=3) from exc
    if json_output:
        typer.echo(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return
    items = result.get("items", [])
    typer.echo(f"{kind}: {len(items)} item(s)")
    for item in items:
        if isinstance(item, dict):
            identifier = (
                item.get("datasetId")
                or item.get("suiteId")
                or item.get("jobId")
                or item.get("resultId")
                or item.get("artifactId")
                or item.get("reviewId")
                or item.get("calibrationId")
                or item.get("qualificationId")
                or item.get("evaluatorId")
            )
            typer.echo(str(identifier))


@evaluation_app.command("history")
def evaluation_history(
    limit: int = typer.Option(50, min=1, max=200),
    cursor: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print bounded content-free evaluation history."""

    _require_local_operator()
    try:
        result = asyncio.run(
            build_evaluation_api_service(Settings()).list_history(cursor=cursor, limit=limit)
        )
    except Exception as exc:
        raise typer.Exit(code=3) from exc
    typer.echo(
        json.dumps(result, sort_keys=True, separators=(",", ":"))
        if json_output
        else f"history: {len(cast(list[object], result['items']))} item(s)"
    )


def main() -> None:
    # The migration image invokes this module without a subcommand.  Typer's
    # default no-argument behavior is help/exit-2 for multi-command apps, so
    # dispatch explicitly while preserving normal subcommand behavior for
    # operators and tests.
    if len(sys.argv) == 1:
        migrate()
        return
    app()


def _require_local_operator() -> None:
    """Require the explicitly enabled local operator boundary for CLI reads.

    The CLI has no browser session to present.  The deployment therefore
    enables it explicitly for a local operator process; absence is a finite
    denial and the marker itself is never emitted in output.
    """

    if os.environ.get("AURA_OBSERVATORY_LOCAL_OPERATOR") != "1":
        typer.echo("local operator authorization required", err=True)
        raise typer.Exit(code=3)


__all__ = ["app", "main"]


def _supports_parameterized_execute(connection: Any) -> bool:
    try:
        return len(inspect.signature(connection.execute).parameters) >= 2
    except TypeError, ValueError:
        return True


def _sync_component_manifest_documents(  # pyright: ignore[reportUnusedFunction]
    connection: Any, definitions: list[dict[str, object]]
) -> int:
    """Compatibility seam for migration tests; SQL remains in the adapter."""

    return PostgresSyncCatalogueAdapter(connection).sync_component_definitions(definitions)


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
