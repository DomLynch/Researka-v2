#!/usr/bin/env python3
"""Create short-lived OSF OAuth authorization links for Researka agents."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent_ids", nargs="+", help="agent IDs to connect to OSF, e.g. agent-v3-full-paper")
    parser.add_argument("--publication-id", help="optional publication to DOI-backfill after OAuth callback")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser.parse_args()


def main() -> int:
    from runtime_core.osf import build_oauth_authorization_url, oauth_config_from_env, sign_oauth_state

    args = parse_args()
    config = oauth_config_from_env()
    if config is None:
        print("OSF OAuth is not configured; check RESEARKA_V2_OSF_OAUTH_* env vars", file=sys.stderr)
        return 2
    if args.publication_id and len(args.agent_ids) != 1:
        print("--publication-id can only be used with one agent_id", file=sys.stderr)
        return 2
    records: list[dict[str, Any]] = []
    for agent_id in args.agent_ids:
        state = sign_oauth_state(agent_id=agent_id, secret=config.state_secret, publication_id=args.publication_id)
        records.append(
            {
                "agent_id": agent_id,
                "expires_in_seconds": 900,
                "authorization_url": build_oauth_authorization_url(config, state=state),
            }
        )

    if args.json:
        print(json.dumps({"records": records}, indent=2))
    else:
        for record in records:
            print(f"{record['agent_id']}: {record['authorization_url']}")
            print(f"  expires_in_seconds: {record['expires_in_seconds']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
