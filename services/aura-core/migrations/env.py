"""Alembic environment for Aura Core-owned schema."""

from logging.config import fileConfig

from alembic import context
from aura_core.bootstrap.database import metadata
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    logging_sections = {"loggers", "handlers", "formatters"}
    configured_sections = {
        section for section in logging_sections if config.file_config.has_section(section)
    }
    if configured_sections:
        missing_sections = logging_sections - configured_sections
        if missing_sections:
            missing = ", ".join(sorted(missing_sections))
            raise RuntimeError(
                f"Alembic logging configuration is incomplete; missing sections: {missing}"
            )
        fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = metadata()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
