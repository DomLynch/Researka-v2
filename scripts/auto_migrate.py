"""Run Alembic migrations as a pre-start step (e.g. ExecStartPre in systemd).

Usage:
    python scripts/auto_migrate.py          # uses RESEARKA_V2_POSTGRES_DSN
    python scripts/auto_migrate.py DSN      # explicit DSN
"""
from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("auto_migrate")


def main() -> None:
    dsn = sys.argv[1] if len(sys.argv) > 1 else os.getenv("RESEARKA_V2_POSTGRES_DSN")
    if not dsn:
        log.error("No DSN provided. Pass as argument or set RESEARKA_V2_POSTGRES_DSN.")
        sys.exit(1)

    from alembic import command as alembic_command  # type: ignore[attr-defined]
    from alembic.config import Config

    log.info("Running alembic upgrade head ...")
    config = Config(os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini"))
    config.set_main_option("sqlalchemy.url", dsn)
    alembic_command.upgrade(config, "head")
    log.info("Migration step finished.")


if __name__ == "__main__":
    main()
