"""Offline template-gate replay, not a reviewer or a publication simulator."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from contracts import ArticleType
from contracts.submissions import run_submission_template_checks


def replay(path: Path, submission_id: str | None = None) -> dict[str, Any]:
    raw = path.read_bytes()
    data = json.loads(raw)
    if isinstance(data, list):
        if not submission_id:
            raise ValueError("An export requires --submission-id; never guess the parent or revision")
        if not all(isinstance(record, dict) for record in data):
            raise ValueError("Export records must be objects")
        matches = [r for r in data if r.get("type") == "submission" and r.get("id") == submission_id]
        if len(matches) != 1:
            raise ValueError("Expected exactly one matching submission in export")
        record = matches[0]
        payload = {**record["metadata"], "title": record["title"]}
    else:
        if submission_id:
            raise ValueError("A request file cannot authenticate a stored --submission-id")
        payload = data
    if not isinstance(payload, dict):
        raise ValueError("Expected a submission request object or a stored-record export")
    sections, bundle = payload.get("sections"), payload.get("source_bundle")
    if not isinstance(sections, dict) or not sections or not all(isinstance(v, str) for v in sections.values()):
        raise ValueError("Replay requires explicit nonempty sections; it does not infer them from markdown")
    if not isinstance(bundle, list) or not all(isinstance(item, dict) for item in bundle):
        raise ValueError("Replay requires a source_bundle array of objects")
    if not all(isinstance(payload.get(key, ""), str) for key in ("title", "article_type")):
        raise ValueError("title and article_type must be strings")
    article_type = ArticleType(payload.get("article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value))
    results = run_submission_template_checks(
        title=payload.get("title", ""), sections=sections, source_bundle=bundle,
        article_type=article_type.value,
        evidence_bundle=payload.get("evidence_bundle"),
    )
    return {
        "scope": "offline_template_gates_only",
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "submission_id": submission_id,
        "gates": {gate.name: gate.passed for gate in results},
        "failed_gates": [gate.name for gate in results if not gate.passed],
        "not_exercised": ["source_resolution", "source_evidence_match", "review", "delivery", "public_visibility"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("--submission-id")
    args = parser.parse_args()
    try:
        report = replay(args.payload, args.submission_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Invalid replay input: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(bool(report["failed_gates"]))


if __name__ == "__main__":
    raise SystemExit(main())
