import sqlite3
from pathlib import Path

from apps.runtime_api.rate_limits import check_and_incr, ensure_schema, public_registration_enabled


def test_counters_survive_restart(tmp_path: Path) -> None:
    db = tmp_path / "rl.db"

    for _ in range(3):
        assert check_and_incr("ip", "1.2.3.4", limit=3, window="2026-05-27", db_path=db)
    assert not check_and_incr("ip", "1.2.3.4", limit=3, window="2026-05-27", db_path=db)

    assert not check_and_incr("ip", "1.2.3.4", limit=3, window="2026-05-27", db_path=db)


def test_public_registration_flag_reads_without_cache(tmp_path: Path) -> None:
    db = tmp_path / "rl.db"

    ensure_schema(db)
    assert public_registration_enabled(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT OR REPLACE INTO flags VALUES ('public_registration', 0)")
    assert not public_registration_enabled(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT OR REPLACE INTO flags VALUES ('public_registration', 1)")
    assert public_registration_enabled(db)
