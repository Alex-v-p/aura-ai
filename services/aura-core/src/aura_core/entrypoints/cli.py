"""Explicit Core migration and seed entrypoints.

Schema changes are deliberately separate from API startup.  The API only
checks that PostgreSQL is reachable; Compose and operators run ``migrate``
before starting API/worker processes.
"""

import asyncio
from pathlib import Path

import typer
from alembic import command
from alembic.config import Config

from aura_core.domains.interaction.agents.adapters import SqlAgentSeeder
from aura_core.platform.auth import Settings
from aura_core.platform.database.engine import make_engine, session_factory

app = typer.Typer(no_args_is_help=True)


def alembic_config(settings: Settings) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("prepend_sys_path", str(root / "src"))
    # Alembic's synchronous runner uses the psycopg driver even though the
    # application uses SQLAlchemy's async engine with the same URL.
    # ConfigParser treats percent signs as interpolation markers; passwords
    # encoded for a URL may legitimately contain them.
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    return config


@app.command()
def migrate(revision: str = "head") -> None:
    """Apply Core-owned migrations without modifying application data."""

    settings = Settings()
    command.upgrade(alembic_config(settings), revision)


async def _seed_async(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    try:
        await SqlAgentSeeder(session_factory(engine)).seed()
    finally:
        await engine.dispose()


@app.command()
def seed() -> None:
    """Insert the immutable general assistant and model policy if absent."""

    asyncio.run(_seed_async(Settings()))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
