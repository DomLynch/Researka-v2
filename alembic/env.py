from __future__ import annotations

import logging
import os

from alembic import context
from sqlalchemy import create_engine, pool

logger = logging.getLogger("alembic")


def _database_url() -> str:
    url = (
        os.getenv("RESEARKA_V2_POSTGRES_DSN")
        or os.getenv("TEST_POSTGRES_DSN")
        or context.config.get_main_option("sqlalchemy.url")
    )
    if url and url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()


def auto_migrate(dsn: str | None = None) -> None:
    """Run alembic upgrade head programmatically.

    Intended for startup-time migration before uvicorn boots.
    Safe to call even when no migrations are pending.
    """
    from alembic.config import Config
    from alembic import command as alembic_command

    config = Config(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")
    )
    url = dsn or os.getenv("RESEARKA_V2_POSTGRES_DSN")
    if url:
        config.set_main_option("sqlalchemy.url", url)
    try:
        alembic_command.upgrade(config, "head")
        logger.info("alembic upgrade head completed")
    except Exception:
        logger.exception("alembic upgrade head failed")
