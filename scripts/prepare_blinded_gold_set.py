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
from runtime_core.judge_release import (
    calibration_metrics_complete,
    calibration_timeline_valid,
    judge_release_manifest_valid,
    unsigned_calibration_sha256,
)

PROTOCOL_VERSION = "researka-blinded-adjudication-v1"
DECISIONS = ("accept", "revise", "reject")
SYNTHETIC_AGENT_MARKERS = ("benchmark", "fixture", "gold-set", "style-test", "synthetic", "test-agent")
SYNTHETIC_FLAG_KEYS = {"is_benchmark", "is_fixture", "is_synthetic", "synthetic", "test_fixture"}
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


def _model_key(value: object) -> str:
    name = str(value or "").strip().casefold().split("/")[-1].split(":")[0]
    return re.sub(r"[^a-z0-9]", "", name)


def _timestamp(value: object, *, context: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{context}: valid_timestamp_required") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{context}: timezone_required")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > datetime.now(timezone.utc):
        raise ValueError(f"{context}: future_timestamp")
    return parsed


def _judge_release(raw: object) -> dict:
    release = raw if isinstance(raw, dict) else {}
    if not judge_release_manifest_valid(release):
        raise ValueError("valid_target_judge_release_required")
    return release


def _independent_adjudicator(raw: object, *, judge_release: dict, context: str) -> tuple[dict, datetime]:
    adjudicator = raw if isinstance(raw, dict) else {}
    adjudicator_id = str(adjudicator.get("id") or "").strip()
    adjudicator_type = str(adjudicator.get("type") or "").strip()
    model = str(adjudicator.get("model") or "").strip()
    excluded_models = {_model_key(item) for item in judge_release["models"]}
    if (
        not adjudicator_id
        or adjudicator_type not in {"human", "independent_model"}
        or adjudicator.get("qualified") is not True
        or adjudicator.get("judge_release_id") != judge_release["id"]
        or not adjudicator.get("qualification_statement")
        or not adjudicator.get("conflict_disclosure")
        or adjudicator.get("reviewer_outputs_hidden_until_freeze") is not True
        or _model_key(adjudicator_id) in excluded_models
        or (model and _model_key(model) in excluded_models)
        or (adjudicator_type == "independent_model" and not model)
    ):
        raise ValueError(f"{context}: independent_adjudicator_provenance_required")
    return adjudicator, _timestamp(adjudicator.get("completed_at"), context=context)


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
            redacted = re.sub(re.escape(identity), "[BLINDED]", redacted, flags=re.I)
    redacted = re.sub(r"\b\d{4}-\d{4}-\d{4}-[\dX]{4}\b", "[BLINDED ORCID]", redacted, flags=re.I)
    redacted = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Z]{2,}\b", "[BLINDED EMAIL]", redacted, flags=re.I)
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


def _contains_synthetic_marker(metadata: dict) -> bool:
    pending: list[object] = [metadata]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).strip().lower()
                if normalized.startswith("_benchmark_"):
                    return True
                if normalized in SYNTHETIC_FLAG_KEYS and (
                    item is True or str(item).strip().lower() in {"1", "true", "yes"}
                ):
                    return True
                if normalized.endswith(("agent_id", "producer_id")) and any(
                    marker in str(item).lower() for marker in SYNTHETIC_AGENT_MARKERS
                ):
                    return True
                pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)
    return False


def candidate_from_row(row: dict) -> dict | None:
    metadata = _metadata(row.get("metadata"))
    article_type = str(metadata.get("article_type") or "").strip()
    domain_slug = str(metadata.get("domain_slug") or "general").strip().lower()
    sources = metadata.get("source_bundle")
    if (
        article_type not in {item.value for item in ArticleType}
        or domain_slug in EXCLUDED_DOMAINS
        or _contains_synthetic_marker(metadata)
        or not isinstance(sources, list)
        or len(sources) < 3
    ):
        return None

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
    identities = {str(metadata.get(key)).strip() for key in IDENTITY_KEYS if metadata.get(key)}
    identity_text = json.dumps(payload, ensure_ascii=False)
    identities.update(
        match.group(1).strip()
        for match in re.finditer(
            r"(?i)\b(?:author|submitted by|affiliation|institution)\s*:\s*([^,\n\"}]+)",
            identity_text,
        )
    )
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
    judge_release: dict,
) -> dict:
    target_release = _judge_release(judge_release)
    selected = select_candidates(candidates, size=size, seed=seed)
    supported_types = {item.value for item in ArticleType}
    sampled_types = {item["article_type"] for item in selected}
    missing_types = sorted(supported_types - sampled_types)
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
        "judge_release": target_release,
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
            "judge_release": target_release,
            "instructions": "Complete every adjudication independently. Do not seek platform verdicts or the other packet.",
            "rubric_keys": sorted(GOLD_SET_RUBRIC_KEYS),
            "adjudicator": {
                "id": "",
                "qualified": False,
                "qualification_statement": "",
                "conflict_disclosure": "",
                "reviewer_outputs_hidden_until_freeze": False,
                "judge_release_id": target_release["id"],
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
        "target_judge_release_id": target_release["id"],
        "blinded_packet_sha256": [_file_sha256(path) for path in packet_paths],
        "historical_decision_strata": dict(sorted(Counter(item["historical_decision"] for item in selected).items())),
        "supported_article_types": sorted(supported_types),
        "covered_article_types": sorted(sampled_types),
        "article_type_counts": dict(sorted(Counter(item["article_type"] for item in selected).items())),
        "missing_article_types": missing_types,
        "article_type_coverage_complete": not missing_types,
        "domain_counts": dict(sorted(Counter(item["domain_slug"] for item in selected).items())),
        "private_material_committed": False,
        "corpus_status": "blinded_pending_adjudication" if not missing_types else "blinded_incomplete_coverage",
    }
    _write_json(receipt_path, receipt)
    return receipt


def _validated_labels(
    path: Path,
    manifest: dict,
) -> tuple[dict, datetime, dict[str, tuple[dict, GoldSetExpectation]]]:
    packet = _load_json(path)
    if packet.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError(f"{path}: protocol_version_mismatch")
    if packet.get("sampling_manifest_sha256") != _sha256(manifest):
        raise ValueError(f"{path}: manifest_hash_mismatch")
    target_release = _judge_release(manifest.get("judge_release"))
    if packet.get("judge_release") != target_release:
        raise ValueError(f"{path}: judge_release_mismatch")
    adjudicator, completed_at = _independent_adjudicator(
        packet.get("adjudicator"),
        judge_release=target_release,
        context=str(path),
    )
    if completed_at < _timestamp(manifest.get("created_at"), context="manifest"):
        raise ValueError(f"{path}: adjudication_predates_freeze")

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
    return adjudicator, completed_at, labels


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
    judge_release: dict,
    manifest_sha: str,
    conflict_ids: set[str],
    adjudicator_ids: set[str],
    labels_frozen_at: datetime,
) -> tuple[dict[str, GoldSetExpectation], str | None, datetime]:
    if not conflict_ids:
        return {}, None, datetime.now(timezone.utc)
    if path is None:
        raise ValueError("conflicts_require_resolution_record")
    record = _load_json(path)
    if record.get("sampling_manifest_sha256") != manifest_sha:
        raise ValueError("resolution_manifest_hash_mismatch")
    method = record.get("method")
    resolver_raw = record.get("resolver")
    resolver: dict[str, object] = resolver_raw if isinstance(resolver_raw, dict) else {}
    resolver_id = str(resolver.get("id") or "").strip()
    labels_revealed_at = _timestamp(record.get("labels_revealed_at"), context="resolution")
    if method == "third_adjudicator":
        try:
            resolver, resolved_at = _independent_adjudicator(
                resolver,
                judge_release=judge_release,
                context="resolver",
            )
        except ValueError as exc:
            raise ValueError("qualified_independent_third_adjudicator_required") from exc
        if resolver_id.casefold() in adjudicator_ids:
            raise ValueError("qualified_independent_third_adjudicator_required")
    elif method == "documented_consensus":
        participants = {str(item).strip().casefold() for item in record.get("participant_ids") or []}
        resolved_at = _timestamp(record.get("completed_at"), context="consensus")
        if not adjudicator_ids.issubset(participants):
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
    if not labels_frozen_at <= labels_revealed_at <= resolved_at:
        raise ValueError("invalid_conflict_resolution_timeline")
    return labels, _file_sha256(path), labels_revealed_at


def merge_adjudications(
    *,
    manifest_path: Path,
    receipt_path: Path,
    label_paths: tuple[Path, Path],
    resolution_path: Path | None,
    output_path: Path,
) -> GoldSetCorpus:
    manifest = _load_json(manifest_path)
    receipt = _load_json(receipt_path)
    manifest_sha = _sha256(manifest)
    if receipt.get("sampling_manifest_sha256") != manifest_sha or receipt.get("case_count") != len(manifest["cases"]):
        raise ValueError("freeze_receipt_does_not_match_manifest")
    target_release = _judge_release(manifest.get("judge_release"))
    first_adjudicator, first_completed_at, first = _validated_labels(label_paths[0], manifest)
    second_adjudicator, second_completed_at, second = _validated_labels(label_paths[1], manifest)
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
    labels_frozen_at = max(first_completed_at, second_completed_at)
    resolutions, resolution_sha, labels_revealed_at = _resolution_labels(
        resolution_path,
        judge_release=target_release,
        manifest_sha=manifest_sha,
        conflict_ids=conflicts,
        adjudicator_ids=adjudicator_ids,
        labels_frozen_at=labels_frozen_at,
    )
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
    corpus = GoldSetCorpus(
        version="gold-set-v2-adjudicated",
        entries=entries,
        adjudication=GoldSetAdjudication(
            corpus_status="adjudicated",
            protocol_version=PROTOCOL_VERSION,
            sampling_manifest_sha256=manifest_sha,
            target_judge_release_id=target_release["id"],
            blinded_packet_sha256=list(receipt["blinded_packet_sha256"]),
            adjudicator_ids=sorted(adjudicator_ids),
            label_file_sha256=[_file_sha256(path) for path in label_paths],
            conflict_count=len(conflicts),
            resolution_file_sha256=resolution_sha,
            inter_adjudicator_decision_agreement=round(observed_agreement, 3),
            inter_adjudicator_kappa=decision_kappa,
            labels_frozen_at=labels_frozen_at,
            labels_revealed_at=labels_revealed_at,
        ),
    )
    _write_json(output_path, corpus.model_dump(mode="json", exclude_none=True))
    return corpus


def sign_evaluation(
    artifact_path: Path,
    *,
    active_release_path: Path,
    signed_by: str,
    statement: str,
) -> dict:
    artifact = _load_json(artifact_path)
    run_meta = artifact.get("run_meta")
    release = run_meta.get("judge_release") if isinstance(run_meta, dict) else None
    release_id = str(run_meta.get("judge_release_id") or "") if isinstance(run_meta, dict) else ""
    if (
        not isinstance(run_meta, dict)
        or run_meta.get("corpus_status") != "adjudicated"
        or run_meta.get("judge_release_consistent") is not True
        or run_meta.get("human_signoff")
        or not isinstance(release, dict)
        or not judge_release_manifest_valid(release)
        or release.get("id") != release_id
        or len(artifact.get("results") or []) < 100
        or not calibration_metrics_complete(artifact)
        or not calibration_timeline_valid(artifact)
        or not artifact.get("limitations")
        or not signed_by.strip()
        or not statement.strip()
    ):
        raise ValueError("evaluation_not_ready_for_human_signoff")
    run_meta["human_signoff"] = {
        "approved": True,
        "results_reviewed": True,
        "limitations_reviewed": True,
        "evaluation_sha256": unsigned_calibration_sha256(artifact),
        "judge_release_id": release_id,
        "signed_by": signed_by.strip(),
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "statement": statement.strip(),
    }
    _write_json(artifact_path, artifact, private=True)
    active_release = dict(release)
    active_release["evaluation"] = {
        "artifact": artifact_path.name,
        "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    }
    _write_json(active_release_path, active_release, private=True)
    return run_meta["human_signoff"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare or merge a blinded real-submission calibration corpus.")
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample")
    sample.add_argument("--size", type=int, default=120)
    sample.add_argument("--seed", required=True)
    sample.add_argument("--judge-release", type=Path, required=True)
    sample.add_argument("--out-dir", type=Path, default=Path("calibration/private/real-v1"))
    sample.add_argument("--public-receipt", type=Path, default=Path("calibration/real_gold_set_v1_freeze_receipt.json"))

    merge = commands.add_parser("merge")
    merge.add_argument("--manifest", type=Path, required=True)
    merge.add_argument("--freeze-receipt", type=Path, required=True)
    merge.add_argument("--labels", type=Path, nargs=2, required=True)
    merge.add_argument("--resolutions", type=Path)
    merge.add_argument("--output", type=Path, default=Path("calibration/gold_set_v2.json"))
    sign = commands.add_parser("sign")
    sign.add_argument("--artifact", type=Path, required=True)
    sign.add_argument("--active-release", type=Path, required=True)
    sign.add_argument("--signed-by", required=True)
    sign.add_argument("--statement", required=True)
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
            judge_release=_load_json(args.judge_release),
        )
        print(json.dumps(receipt, indent=2))
        return
    if args.command == "sign":
        signoff = sign_evaluation(
            args.artifact,
            active_release_path=args.active_release,
            signed_by=args.signed_by,
            statement=args.statement,
        )
        print(json.dumps(signoff, indent=2))
        return
    corpus = merge_adjudications(
        manifest_path=args.manifest,
        receipt_path=args.freeze_receipt,
        label_paths=tuple(args.labels),
        resolution_path=args.resolutions,
        output_path=args.output,
    )
    print(f"adjudicated_cases={len(corpus.entries)}")


if __name__ == "__main__":
    main()
