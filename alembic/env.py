"""
Alembic environment configuration for Famosi.

Uses an async engine (asyncpg) so that the app's async SQLAlchemy setup is
consistent with what Alembic runs during migration generation and execution.

DATABASE_URL is read from the application's Settings object (which in turn
reads from the `.env` file via pydantic-settings).

All models are imported via `app.models` so that `autogenerate` can detect
every table and enum defined in the ORM.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ---------------------------------------------------------------------------
# Load app config — this reads DATABASE_URL from .env
# ---------------------------------------------------------------------------
from app.config import settings

# ---------------------------------------------------------------------------
# Import ALL models so that autogenerate sees every table / type
# ---------------------------------------------------------------------------
import app.models  # noqa: F401 — side-effect import registers all metadata
from app.models.base import Base

# ---------------------------------------------------------------------------
# Alembic Config object (gives access to alembic.ini values)
# ---------------------------------------------------------------------------
config = context.config

# Override the sqlalchemy.url from alembic.ini with the value from Settings
# so we never need to duplicate credentials in alembic.ini.
config.set_main_option("sqlalchemy.url", settings.database_url)

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata target for autogenerate
target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Run migrations without a live DB connection (offline mode)
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    """
    Run migrations without a live database connection.

    Emits the SQL to stdout (useful for review before executing).
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Run migrations with a live async DB connection (online mode)
# ---------------------------------------------------------------------------
def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations through a sync proxy."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
