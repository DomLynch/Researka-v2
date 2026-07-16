#!/usr/bin/env python3
"""Reconcile historical publication visibility and classification. Dry-run by default."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts import ObjectType  # noqa: E402
from contracts.submissions import run_submission_template_checks  # noqa: E402
from runtime_core.evidence_quality import evidence_profile, publication_class  # noqa: E402
from runtime_core.repos import (  # noqa: E402
    PostgresRuntimeRepository,
    RuntimeRepository,
    postgres_dsn_from_env,
)


def reconcile_publications(
    repo: RuntimeRepository,
    *,
    publication_ids: set[str] | None = None,
    apply: bool = False,
    audited_at: str | None = None,
) -> dict:
    audited_at = audited_at or datetime.now(timezone.utc).isoformat()
    items = []
    for publication in repo.list_objects(ObjectType.PUBLICATION):
        if publication_ids and publication.id not in publication_ids:
            continue
        submission_id = str(publication.metadata.get("source_submission_id") or "")
        submission = repo.get_object(submission_id) if submission_id else None
        bundle = list((submission.metadata if submission else {}).get("source_bundle") or [])
        if not submission or not bundle:
            items.append({"publication_id": publication.id, "status": "skipped_missing_submission_or_bundle"})
            continue
        article_type = str(submission.metadata.get("article_type") or publication.metadata.get("article_type") or "")
        checks = run_submission_template_checks(
            title=submission.title,
            sections=dict(submission.metadata.get("sections") or {}),
            source_bundle=bundle,
            article_type=article_type,
        )
        unsafe_role = any(check.name == "source_role" and not check.passed for check in checks)
        profile = evidence_profile(text=publication.body_markdown, source_bundle=bundle)
        target_class = publication_class(article_type=article_type, title=publication.title, profile=profile)
        updates: dict[str, object] = {"evidence_profile": profile}
        if target_class != publication.metadata.get("publication_class"):
            updates["publication_class"] = target_class
        if unsafe_role and publication.metadata.get("public_visibility", "listed") != "hidden":
            updates["public_visibility"] = "hidden"
        changed = any(publication.metadata.get(key) != value for key, value in updates.items())
        status = "would_hide" if unsafe_role and changed else "would_reclassify" if changed else "unchanged"
        if apply and changed:
            prior_audit = publication.metadata.get("quality_audit")
            prior_audit = prior_audit if isinstance(prior_audit, dict) else {}
            updates["quality_audit"] = {
                "audited_at": audited_at,
                "reason": "non_load_bearing_primary_source" if unsafe_role else "current_publication_contract",
                "previous_public_visibility": prior_audit.get(
                    "previous_public_visibility", publication.metadata.get("public_visibility", "listed")
                ),
                "previous_publication_class": prior_audit.get(
                    "previous_publication_class", publication.metadata.get("publication_class")
                ),
            }
            repo.update_object_metadata(publication.id, {**publication.metadata, **updates})
            status = "hidden" if unsafe_role else "reclassified"
        items.append(
            {
                "publication_id": publication.id,
                "status": status,
                "publication_class": target_class,
                "public_visibility": "hidden" if unsafe_role else publication.metadata.get("public_visibility", "listed"),
            }
        )
    return {"mode": "apply" if apply else "dry_run", "processed": len(items), "items": items}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--all", action="store_true", help="explicitly process every publication")
    parser.add_argument("--publication-id", action="append", dest="publication_ids")
    parser.add_argument("--dsn", default=None)
    args = parser.parse_args()
    if args.apply and not (args.all or args.publication_ids):
        parser.error("--apply requires --publication-id or --all")
    dsn = args.dsn or postgres_dsn_from_env()
    if not dsn:
        print("RESEARKA_V2_POSTGRES_DSN is required", file=sys.stderr)
        return 2
    summary = reconcile_publications(
        PostgresRuntimeRepository(dsn),
        publication_ids=set(args.publication_ids or []) or None,
        apply=args.apply,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
