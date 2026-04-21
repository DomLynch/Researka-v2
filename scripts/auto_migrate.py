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

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))
    from alembic.env import auto_migrate

    log.info("Running alembic upgrade head ...")
    auto_migrate(dsn=dsn)
    log.info("Migration step finished.")


if __name__ == "__main__":
    main()
