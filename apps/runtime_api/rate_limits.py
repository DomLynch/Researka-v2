from __future__ import annotations

import os
import sqlite3
from pathlib import Path


DB_PATH = Path("/var/lib/researka-v2/rate_limits.db")


def _db_path(db_path: str | os.PathLike[str] | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    return Path(os.environ.get("RESEARKA_V2_RATE_LIMIT_DB_PATH", str(DB_PATH)))


def ensure_schema(db_path: str | os.PathLike[str] | None = None) -> None:
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rl_counters (
              scope TEXT NOT NULL,
              key TEXT NOT NULL,
              window TEXT NOT NULL,
              count INTEGER NOT NULL,
              PRIMARY KEY (scope, key, window)
            )
            """
        )
        conn.execute("CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, enabled INTEGER NOT NULL)")


def check_and_incr(
    scope: str,
    key: str,
    *,
    limit: int,
    window: str,
    db_path: str | os.PathLike[str] | None = None,
) -> bool:
    """Atomic. Returns True if allowed and incremented, False if over limit."""
    if limit <= 0:
        return False
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rl_counters (
                  scope TEXT NOT NULL,
                  key TEXT NOT NULL,
                  window TEXT NOT NULL,
                  count INTEGER NOT NULL,
                  PRIMARY KEY (scope, key, window)
                )
                """
            )
            row = conn.execute(
                "SELECT count FROM rl_counters WHERE scope=? AND key=? AND window=?",
                (scope, key, window),
            ).fetchone()
            current = row[0] if row else 0
            if current >= limit:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "INSERT OR REPLACE INTO rl_counters VALUES (?,?,?,?)",
                (scope, key, window, current + 1),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def public_registration_enabled(db_path: str | os.PathLike[str] | None = None) -> bool:
    ensure_schema(db_path)
    with sqlite3.connect(_db_path(db_path), isolation_level=None) as conn:
        row = conn.execute("SELECT enabled FROM flags WHERE name='public_registration'").fetchone()
    return not (row and row[0] == 0)
