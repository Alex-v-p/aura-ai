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
    for path in clickhouse:
        response = httpx.post(
            settings.clickhouse_url,
            params={"database": "aura_observatory"},
            content=path.read_text(encoding="utf-8"),
            timeout=10.0,
        )
        response.raise_for_status()
    typer.echo(f"aura-observatory migrations applied ({revision})")


def main() -> None:
    app()


if __name__ == "__main__":
    main()


__all__ = ["app", "main"]
