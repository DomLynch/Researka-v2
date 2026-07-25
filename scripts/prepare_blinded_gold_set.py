#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import (
    GOLD_SET_RUBRIC_KEYS,
    ArticleType,
    GoldSetAdjudication,
    GoldSetCorpus,
    GoldSetEntry,
    GoldSetExpectation,
    SubmissionPayload,
)

PROTOCOL_VERSION = "researka-blinded-adjudication-v1"
DECISIONS = ("accept", "revise", "reject")
SYNTHETIC_AGENT_MARKERS = ("benchmark", "fixture", "gold-set", "style-test", "synthetic", "test-agent")
EXCLUDED_DOMAINS = {"ops", "test", "x"}
IDENTITY_KEYS = (
    "author_name",
    "author_agent_id",
    "authenticated_agent_id",
    "claimed_author_agent_id",
    "agent_id",
    "author_signature",
    "institution_name",
    "institution_ror",
    "ror_id",
    "raid_id",
    "orcid",
)

_CANDIDATE_SQL = """
WITH latest_decision AS (
  SELECT DISTINCT ON (parent_object_id)
    parent_object_id,
    lower(coalesce(metadata::jsonb->>'decision', '')) AS decision,
    created_at
  FROM research_objects
  WHERE object_type = 'decision'
    AND coalesce((metadata::jsonb->>'superseded')::boolean, false) = false
  ORDER BY parent_object_id, created_at DESC
)
SELECT s.id, s.title, s.body_markdown, s.metadata, s.created_at, d.decision
FROM research_objects s
JOIN latest_decision d ON d.parent_object_id = s.id
WHERE s.object_type = 'submission'
  AND d.decision IN ('accept', 'revise', 'reject')
ORDER BY s.created_at DESC
"""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _file_sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _load_json(path: Path) -> dict:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected_json_object")
    return raw


def _write_json(path: Path, value: object, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        path.parent.chmod(0o700)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    if private:
        path.chmod(0o600)


def _metadata(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    return {}


def _redact_text(text: str, identities: set[str]) -> str:
    redacted = text
    for identity in sorted(identities, key=len, reverse=True):
        if len(identity) >= 4:
            redacted = redacted.replace(identity, "[BLINDED]")
    redacted = re.sub(r"\b\d{4}-\d{4}-\d{4}-[\dX]{4}\b", "[BLINDED ORCID]", redacted, flags=re.I)
    return re.sub(
        r"(?im)^(?:author agent|submitted by|orcid|affiliation|institution)\s*:\s*.*$",
        "[BLINDED IDENTITY]",
        redacted,
    )


def _redact(value: object, identities: set[str]) -> object:
    if isinstance(value, str):
        return _redact_text(value, identities)
    if isinstance(value, list):
        return [_redact(item, identities) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, identities) for key, item in value.items()}
    return value


def candidate_from_row(row: dict) -> dict | None:
    metadata = _metadata(row.get("metadata"))
    article_type = str(metadata.get("article_type") or "").strip()
    domain_slug = str(metadata.get("domain_slug") or "general").strip().lower()
    agent_id = str(metadata.get("authenticated_agent_id") or metadata.get("author_agent_id") or "").lower()
    sources = metadata.get("source_bundle")
    if (
        article_type not in {item.value for item in ArticleType}
        or domain_slug in EXCLUDED_DOMAINS
        or any(marker in agent_id for marker in SYNTHETIC_AGENT_MARKERS)
        or not isinstance(sources, list)
        or len(sources) < 3
    ):
        return None

    identities = {str(metadata.get(key)).strip() for key in IDENTITY_KEYS if metadata.get(key)}
    identity_text = "\n".join(
        [
            str(metadata.get("abstract") or ""),
            str(metadata.get("body_markdown") or row.get("body_markdown") or ""),
        ]
    )
    identities.update(
        match.group(1).strip()
        for match in re.finditer(
            r"(?im)^(?:author|submitted by|affiliation|institution)\s*:\s*(.+)$",
            identity_text,
        )
    )
    payload: dict[str, object] = {
        "title": row.get("title") or metadata.get("title") or "Untitled submission",
        "abstract": metadata.get("abstract") or row.get("body_markdown") or "",
        "body_markdown": metadata.get("body_markdown") or row.get("body_markdown") or None,
        "sections": metadata.get("sections") if isinstance(metadata.get("sections"), dict) else {},
        "source_bundle": sources,
        "author_agent_id": "blinded-adjudication",
        "article_type": article_type,
        "artifact_type": metadata.get("artifact_type"),
        "topic": metadata.get("topic"),
        "novelty_score": metadata.get("novelty_score"),
        "confidence_score": metadata.get("confidence_score"),
        "domain_slug": domain_slug,
        "core_claims_resolved": bool(metadata.get("core_claims_resolved", True)),
        "submitted_at": "2000-01-01T00:00:00Z",
    }
    blinded = SubmissionPayload.model_validate(_redact(payload, identities)).model_dump(mode="json", exclude_none=True)
    content = " ".join(
        [
            str(blinded.get("abstract") or ""),
            str(blinded.get("body_markdown") or ""),
            " ".join(str(item) for item in blinded.get("sections", {}).values()),
        ]
    )
    if len(content.strip()) < 200:
        return None
    source_hash = str(metadata.get("submission_content_hash") or _sha256(payload))
    return {
        "source_submission_id": str(row["id"]),
        "source_content_sha256": source_hash,
        "historical_decision": str(row["decision"]),
        "article_type": article_type,
        "domain_slug": domain_slug,
        "created_at": str(row.get("created_at") or ""),
        "submission": blinded,
        "blinded_content_sha256": _sha256(blinded),
    }


def fetch_candidates(dsn: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cursor:
        cursor.execute(_CANDIDATE_SQL)
        candidates = [candidate_from_row(dict(row)) for row in cursor.fetchall()]
    unique: dict[str, dict] = {}
    for candidate in candidates:
        if candidate is not None:
            unique.setdefault(candidate["source_content_sha256"], candidate)
    return list(unique.values())


def _deterministic_order(items: list[dict], seed: str) -> list[dict]:
    return sorted(items, key=lambda item: _sha256(f"{seed}:{item['source_submission_id']}"))


def _round_robin(items: list[dict], quota: int, seed: str) -> list[dict]:
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in _deterministic_order(items, seed):
        buckets[(item["article_type"], item["domain_slug"])].append(item)
    selected: list[dict] = []
    for article_type in sorted({item["article_type"] for item in items}):
        candidates = _deterministic_order(
            [item for item in items if item["article_type"] == article_type],
            f"{seed}:{article_type}",
        )
        if candidates and len(selected) < quota:
            candidate = candidates[0]
            selected.append(candidate)
            buckets[(candidate["article_type"], candidate["domain_slug"])].remove(candidate)
    keys = sorted(
        (key for key, bucket in buckets.items() if bucket),
        key=lambda key: _sha256(f"{seed}:{key[0]}:{key[1]}"),
    )
    while keys and len(selected) < quota:
        next_keys: list[tuple[str, str]] = []
        for key in keys:
            if buckets[key] and len(selected) < quota:
                selected.append(buckets[key].pop(0))
            if buckets[key]:
                next_keys.append(key)
        keys = next_keys
    if len(selected) != quota:
        raise ValueError(f"insufficient_candidates:{len(selected)}/{quota}")
    return selected


def select_candidates(candidates: list[dict], *, size: int, seed: str) -> list[dict]:
    if size < 100:
        raise ValueError("calibration_sample_requires_at_least_100_cases")
    base, remainder = divmod(size, len(DECISIONS))
    selected: list[dict] = []
    for index, decision in enumerate(DECISIONS):
        quota = base + (1 if index < remainder else 0)
        decision_candidates = [item for item in candidates if item["historical_decision"] == decision]
        selected.extend(_round_robin(decision_candidates, quota, f"{seed}:{decision}"))
    if {item["article_type"] for item in selected} != {item["article_type"] for item in candidates}:
        raise ValueError("sample_does_not_span_available_real_article_types")
    if len({item["domain_slug"] for item in selected}) < 8:
        raise ValueError("sample_requires_at_least_eight_domains")
    return _deterministic_order(selected, seed)


def freeze_candidates(
    candidates: list[dict],
    *,
    out_dir: Path,
    receipt_path: Path,
    size: int,
    seed: str,
) -> dict:
    selected = select_candidates(candidates, size=size, seed=seed)
    sampled_types = {item["article_type"] for item in selected}
    missing_types = sorted({item.value for item in ArticleType} - sampled_types)
    created_at = datetime.now(timezone.utc).isoformat()
    cases = []
    manifest_cases = []
    for item in selected:
        source_key = f"{seed}:{item['source_submission_id']}"
        case_id = f"case-{hashlib.sha256(source_key.encode()).hexdigest()[:16]}"
        cases.append(
            {
                "case_id": case_id,
                "blinded_content_sha256": item["blinded_content_sha256"],
                "article_type": item["article_type"],
                "domain_slug": item["domain_slug"],
                "submission": item["submission"],
                "adjudication": {
                    "decision": None,
                    "rubric_scores": {},
                    "claim_support_verdict": None,
                    "overclaim_verdict": None,
                    "synthesis_quality_verdict": None,
                    "rationale": "",
                },
            }
        )
        manifest_cases.append(
            {
                "case_id": case_id,
                "source_submission_id": item["source_submission_id"],
                "source_content_sha256": item["source_content_sha256"],
                "blinded_content_sha256": item["blinded_content_sha256"],
                "historical_decision": item["historical_decision"],
                "article_type": item["article_type"],
                "domain_slug": item["domain_slug"],
            }
        )

    manifest = {
        "protocol_version": PROTOCOL_VERSION,
        "created_at": created_at,
        "seed": seed,
        "case_count": len(cases),
        "sampling_rules": {
            "source": "production submissions with final decisions",
            "historical_decision_quota": "balanced and hidden from adjudicators",
            "minimum_sources": 3,
            "excluded_domains": sorted(EXCLUDED_DOMAINS),
            "synthetic_agent_markers": list(SYNTHETIC_AGENT_MARKERS),
        },
        "cases": manifest_cases,
    }
    manifest_sha = _sha256(manifest)
    manifest_path = out_dir / "private_manifest.json"
    _write_json(manifest_path, manifest, private=True)

    packet_paths: list[Path] = []
    for packet_id in ("a", "b"):
        ordered = sorted(cases, key=lambda case: _sha256(f"{seed}:packet-{packet_id}:{case['case_id']}"))
        packet = {
            "protocol_version": PROTOCOL_VERSION,
            "packet_id": packet_id,
            "sampling_manifest_sha256": manifest_sha,
            "instructions": "Complete every adjudication independently. Do not seek platform verdicts or the other packet.",
            "rubric_keys": sorted(GOLD_SET_RUBRIC_KEYS),
            "adjudicator": {
                "id": "",
                "qualified": False,
                "qualification_statement": "",
                "conflict_disclosure": "",
                "completed_at": None,
            },
            "cases": ordered,
        }
        path = out_dir / f"adjudicator-{packet_id}.json"
        _write_json(path, packet, private=True)
        packet_paths.append(path)

    receipt = {
        "protocol_version": PROTOCOL_VERSION,
        "created_at": created_at,
        "case_count": len(cases),
        "sampling_manifest_sha256": manifest_sha,
        "blinded_packet_sha256": [_file_sha256(path) for path in packet_paths],
        "historical_decision_strata": dict(sorted(Counter(item["historical_decision"] for item in selected).items())),
        "article_type_counts": dict(sorted(Counter(item["article_type"] for item in selected).items())),
        "missing_article_types": missing_types,
        "article_type_coverage_complete": not missing_types,
        "domain_counts": dict(sorted(Counter(item["domain_slug"] for item in selected).items())),
        "private_material_committed": False,
        "corpus_status": "blinded_pending_adjudication" if not missing_types else "blinded_incomplete_coverage",
    }
    _write_json(receipt_path, receipt)
    return receipt


def _validated_labels(path: Path, manifest: dict) -> tuple[dict, dict[str, tuple[dict, GoldSetExpectation]]]:
    packet = _load_json(path)
    if packet.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError(f"{path}: protocol_version_mismatch")
    if packet.get("sampling_manifest_sha256") != _sha256(manifest):
        raise ValueError(f"{path}: manifest_hash_mismatch")
    adjudicator = packet.get("adjudicator")
    if not isinstance(adjudicator, dict) or not adjudicator.get("id") or adjudicator.get("qualified") is not True:
        raise ValueError(f"{path}: qualified_adjudicator_required")
    if (
        not adjudicator.get("completed_at")
        or not adjudicator.get("qualification_statement")
        or not adjudicator.get("conflict_disclosure")
    ):
        raise ValueError(f"{path}: adjudicator_provenance_required")

    expected_hashes = {case["case_id"]: case["blinded_content_sha256"] for case in manifest["cases"]}
    labels: dict[str, tuple[dict, GoldSetExpectation]] = {}
    for case in packet.get("cases", []):
        case_id = str(case.get("case_id") or "")
        submission = case.get("submission")
        if case_id not in expected_hashes or not isinstance(submission, dict):
            raise ValueError(f"{path}: unexpected_or_invalid_case:{case_id}")
        if _sha256(submission) != expected_hashes[case_id]:
            raise ValueError(f"{path}: case_content_changed:{case_id}")
        expectation = _complete_expectation(case.get("adjudication"), context=f"{path}:{case_id}")
        labels[case_id] = (submission, expectation)
    if set(labels) != set(expected_hashes):
        raise ValueError(f"{path}: all_manifest_cases_required")
    return adjudicator, labels


def _complete_expectation(raw: object, *, context: str) -> GoldSetExpectation:
    expectation = GoldSetExpectation.model_validate(raw)
    if set(expectation.rubric_scores) != GOLD_SET_RUBRIC_KEYS or any(
        not 1 <= score <= 5 for score in expectation.rubric_scores.values()
    ):
        raise ValueError(f"{context}: complete_rubric_required")
    if (
        expectation.claim_support_verdict is None
        or expectation.overclaim_verdict is None
        or expectation.synthesis_quality_verdict is None
        or len(expectation.rationale.strip()) < 20
    ):
        raise ValueError(f"{context}: complete_reasoned_label_required")
    return expectation


def _label_signature(expectation: GoldSetExpectation) -> dict:
    return expectation.model_dump(mode="json", exclude={"rationale"})


def _resolution_labels(
    path: Path | None,
    *,
    manifest_sha: str,
    conflict_ids: set[str],
    adjudicator_ids: set[str],
) -> tuple[dict[str, GoldSetExpectation], str | None]:
    if not conflict_ids:
        return {}, None
    if path is None:
        raise ValueError("conflicts_require_resolution_record")
    record = _load_json(path)
    if record.get("sampling_manifest_sha256") != manifest_sha:
        raise ValueError("resolution_manifest_hash_mismatch")
    method = record.get("method")
    resolver_raw = record.get("resolver")
    resolver: dict[str, object] = resolver_raw if isinstance(resolver_raw, dict) else {}
    resolver_id = str(resolver.get("id") or "")
    if method == "third_adjudicator":
        if (
            not resolver.get("qualified")
            or not resolver.get("qualification_statement")
            or not resolver.get("conflict_disclosure")
            or not resolver.get("completed_at")
            or not resolver_id
            or resolver_id.casefold() in adjudicator_ids
        ):
            raise ValueError("qualified_independent_third_adjudicator_required")
    elif method == "documented_consensus":
        participants = {str(item).strip().casefold() for item in record.get("participant_ids") or []}
        if not adjudicator_ids.issubset(participants) or not record.get("completed_at"):
            raise ValueError("consensus_must_include_both_adjudicators")
    else:
        raise ValueError("unsupported_conflict_resolution_method")
    labels = {
        str(item["case_id"]): _complete_expectation(
            item.get("adjudication"),
            context=f"{path}:{item.get('case_id')}",
        )
        for item in record.get("resolutions", [])
    }
    if set(labels) != conflict_ids:
        raise ValueError("resolution_record_must_cover_exact_conflicts")
    return labels, _file_sha256(path)


def merge_adjudications(
    *,
    manifest_path: Path,
    receipt_path: Path,
    label_paths: tuple[Path, Path],
    resolution_path: Path | None,
    signoff_path: Path,
    output_path: Path,
) -> GoldSetCorpus:
    manifest = _load_json(manifest_path)
    receipt = _load_json(receipt_path)
    manifest_sha = _sha256(manifest)
    if receipt.get("sampling_manifest_sha256") != manifest_sha or receipt.get("case_count") != len(manifest["cases"]):
        raise ValueError("freeze_receipt_does_not_match_manifest")
    first_adjudicator, first = _validated_labels(label_paths[0], manifest)
    second_adjudicator, second = _validated_labels(label_paths[1], manifest)
    adjudicator_ids = {
        str(first_adjudicator["id"]).strip().casefold(),
        str(second_adjudicator["id"]).strip().casefold(),
    }
    if len(adjudicator_ids) != 2:
        raise ValueError("two_distinct_adjudicators_required")

    conflicts = {
        case_id
        for case_id in first
        if _label_signature(first[case_id][1]) != _label_signature(second[case_id][1])
    }
    decision_matrix = {expected: {actual: 0 for actual in DECISIONS} for expected in DECISIONS}
    for case_id in first:
        decision_matrix[first[case_id][1].decision.value][second[case_id][1].decision.value] += 1
    decision_total = len(first)
    decision_matches = sum(decision_matrix[label][label] for label in DECISIONS)
    expected_agreement = sum(
        sum(decision_matrix[label].values())
        * sum(decision_matrix[row][label] for row in DECISIONS)
        for label in DECISIONS
    ) / (decision_total * decision_total)
    observed_agreement = decision_matches / decision_total
    decision_kappa = (
        1.0
        if expected_agreement == 1 and observed_agreement == 1
        else round((observed_agreement - expected_agreement) / (1 - expected_agreement), 3)
    )
    resolutions, resolution_sha = _resolution_labels(
        resolution_path,
        manifest_sha=manifest_sha,
        conflict_ids=conflicts,
        adjudicator_ids=adjudicator_ids,
    )
    signoff = _load_json(signoff_path)
    if (
        signoff.get("sampling_manifest_sha256") != manifest_sha
        or signoff.get("approved") is not True
        or not signoff.get("signed_by")
        or not signoff.get("signed_at")
        or not signoff.get("statement")
        or signoff.get("reviewer_outputs_hidden_until_freeze") is not True
    ):
        raise ValueError("valid_human_signoff_required")

    manifest_by_id = {case["case_id"]: case for case in manifest["cases"]}
    entries = []
    for case_id in sorted(first):
        submission, expectation = first[case_id]
        entries.append(
            GoldSetEntry(
                entry_id=case_id,
                article_type=manifest_by_id[case_id]["article_type"],
                submission=SubmissionPayload.model_validate(submission),
                expected=resolutions.get(case_id, expectation),
                tags=[f"domain:{manifest_by_id[case_id]['domain_slug']}", "blinded-real-submission"],
                notes="Independently adjudicated under the blinded calibration protocol.",
            )
        )
    labels_frozen_at = datetime.now(timezone.utc)
    corpus = GoldSetCorpus(
        version="gold-set-v2-adjudicated",
        entries=entries,
        adjudication=GoldSetAdjudication(
            corpus_status="adjudicated",
            protocol_version=PROTOCOL_VERSION,
            sampling_manifest_sha256=manifest_sha,
            blinded_packet_sha256=list(receipt["blinded_packet_sha256"]),
            adjudicator_ids=sorted(adjudicator_ids),
            label_file_sha256=[_file_sha256(path) for path in label_paths],
            conflict_count=len(conflicts),
            resolution_file_sha256=resolution_sha,
            inter_adjudicator_decision_agreement=round(observed_agreement, 3),
            inter_adjudicator_kappa=decision_kappa,
            labels_frozen_at=labels_frozen_at,
            labels_revealed_at=labels_frozen_at,
            human_signoff=signoff,
        ),
    )
    _write_json(output_path, corpus.model_dump(mode="json", exclude_none=True))
    return corpus


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare or merge a blinded real-submission calibration corpus.")
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample")
    sample.add_argument("--size", type=int, default=120)
    sample.add_argument("--seed", required=True)
    sample.add_argument("--out-dir", type=Path, default=Path("calibration/private/real-v1"))
    sample.add_argument("--public-receipt", type=Path, default=Path("calibration/real_gold_set_v1_freeze_receipt.json"))

    merge = commands.add_parser("merge")
    merge.add_argument("--manifest", type=Path, required=True)
    merge.add_argument("--freeze-receipt", type=Path, required=True)
    merge.add_argument("--labels", type=Path, nargs=2, required=True)
    merge.add_argument("--resolutions", type=Path)
    merge.add_argument("--signoff", type=Path, required=True)
    merge.add_argument("--output", type=Path, default=Path("calibration/gold_set_v2.json"))
    args = parser.parse_args()

    if args.command == "sample":
        dsn = os.environ.get("RESEARKA_V2_POSTGRES_DSN")
        if not dsn:
            raise SystemExit("RESEARKA_V2_POSTGRES_DSN is required")
        receipt = freeze_candidates(
            fetch_candidates(dsn),
            out_dir=args.out_dir,
            receipt_path=args.public_receipt,
            size=args.size,
            seed=args.seed,
        )
        print(json.dumps(receipt, indent=2))
        return
    corpus = merge_adjudications(
        manifest_path=args.manifest,
        receipt_path=args.freeze_receipt,
        label_paths=tuple(args.labels),
        resolution_path=args.resolutions,
        signoff_path=args.signoff,
        output_path=args.output,
    )
    print(f"adjudicated_cases={len(corpus.entries)}")


if __name__ == "__main__":
    main()
