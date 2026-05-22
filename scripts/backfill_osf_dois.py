#!/usr/bin/env python3
"""Backfill OSF DOI metadata for existing Researka v2 publications.

Dry-run is the default. Use --apply only after reviewing the JSON summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="create OSF child nodes, mint DOIs, and persist metadata")
    parser.add_argument("--limit", type=int, default=None, help="maximum eligible publications to process")
    parser.add_argument("--publication-id", default=None, help="process one publication id")
    parser.add_argument("--dsn", default=None, help="Postgres DSN; defaults to RESEARKA_V2_POSTGRES_DSN")
    return parser.parse_args()


def main() -> int:
    from runtime_core.osf import backfill_missing_publication_dois, config_from_env
    from runtime_core.repos import PostgresRuntimeRepository, postgres_dsn_from_env

    args = parse_args()
    dsn = args.dsn or postgres_dsn_from_env()
    if not dsn:
        print("RESEARKA_V2_POSTGRES_DSN is required", file=sys.stderr)
        return 2
    config = config_from_env() if args.apply else None

    try:
        repo = PostgresRuntimeRepository(dsn)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    summary = backfill_missing_publication_dois(
        repo,
        apply=args.apply,
        limit=args.limit,
        publication_id=args.publication_id,
        config=config,
    )
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
