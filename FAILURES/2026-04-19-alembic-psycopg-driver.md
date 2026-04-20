# Failure: Alembic defaulted to psycopg2 instead of psycopg3

## Date
2026-04-19

## Trigger
The first clean `alembic upgrade head` run against the local Postgres migration DB.

## Symptom
Alembic failed immediately with `ModuleNotFoundError: No module named 'psycopg2'` even though the repo already had `psycopg[binary]` installed.

## Root cause
SQLAlchemy interpreted a plain `postgresql://` DSN as the `psycopg2` driver path by default. The repo uses psycopg3, so the Alembic env needed to normalize the URL explicitly to `postgresql+psycopg://`.

## Fix
Normalize Postgres DSNs inside `alembic/env.py` before creating the engine, then rerun `alembic upgrade head` on a fresh database.

## Prevention
When adding infrastructure tooling around Postgres, verify the exact SQLAlchemy driver prefix on the first real run instead of assuming the runtime driver will be inferred correctly.

## Related skills
- v4 default stack: evidence-first patching + failure artifact on first real tool break
