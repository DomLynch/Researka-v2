from __future__ import annotations

import json
import time
from pathlib import Path

from contracts import Decision, GoldSetCorpus, GoldSetEntry, ObjectType, ResearchObject, RuntimeJob, Stage

from .repos import InMemoryRuntimeRepository
from .workflow import REVIEW_RUBRIC_KEYS, WorkflowEngine

DECISION_LABELS = [Decision.ACCEPT.value, Decision.REVISE.value, Decision.REJECT.value]
BOOLEAN_FIELDS = (
    "claim_support_verdict",
    "overclaim_verdict",
    "synthesis_quality_verdict",
)


def load_gold_set(path: str) -> GoldSetCorpus:
    raw = json.loads(Path(path).read_text())
    if isinstance(raw, list):
        raw = {"entries": raw}
    return GoldSetCorpus.model_validate(raw)


def _empty_confusion_matrix() -> dict[str, dict[str, int]]:
    return {expected: {actual: 0 for actual in DECISION_LABELS} for expected in DECISION_LABELS}


def _decision_for_intake_reject() -> str:
    return Decision.REJECT.value


def _accept_blockers(review_metadata: dict[str, object]) -> list[str]:
    blockers: list[str] = []
    rubric_scores = review_metadata.get("rubric_scores", {})
    if isinstance(rubric_scores, dict):
        weak_scores = [key for key in REVIEW_RUBRIC_KEYS if int(rubric_scores.get(key, 0) or 0) < 4]
        if weak_scores:
            blockers.append(f"rubric_scores:{','.join(weak_scores)}")
    if review_metadata.get("major_issues"):
        blockers.append("major_issues")
    if review_metadata.get("required_revisions"):
        blockers.append("required_revisions")
    if review_metadata.get("claim_support_verdict") != "supported":
        blockers.append("claim_support_verdict")
    if review_metadata.get("overclaim_verdict") != "none":
        blockers.append("overclaim_verdict")
    if review_metadata.get("synthesis_quality_verdict") not in {"strong", "adequate"}:
        blockers.append("synthesis_quality_verdict")
    return blockers


def run_gold_entry(entry: GoldSetEntry, engine: WorkflowEngine) -> dict:
    repo = InMemoryRuntimeRepository()
    submission_payload = entry.submission.model_dump(mode="json")
    submission_payload["article_type"] = entry.article_type.value

    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title=entry.submission.title,
            body_markdown=entry.submission.abstract,
            metadata=submission_payload,
        )
    )

    record = {
        "entry_id": entry.entry_id,
        "title": entry.submission.title,
        "article_type": entry.article_type.value,
        "domain_slug": entry.submission.domain_slug,
        "expected_decision": entry.expected.decision.value,
        "actual_decision": None,
        "decision_match": False,
        "stage_reached": "submission",
        "route": None,
        "cost_usd": 0.0,
        "duration_s": 0.0,
        "error": None,
        "expected_rubric_scores": entry.expected.rubric_scores,
        "actual_rubric_scores": {},
        "rubric_score_deltas": {},
        "expected_claim_support_verdict": entry.expected.claim_support_verdict,
        "actual_claim_support_verdict": None,
        "expected_overclaim_verdict": entry.expected.overclaim_verdict,
        "actual_overclaim_verdict": None,
        "expected_synthesis_quality_verdict": entry.expected.synthesis_quality_verdict,
        "actual_synthesis_quality_verdict": None,
        "accept_blockers": [],
    }

    start = time.time()
    try:
        intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE))
        intake_result = engine.handle_job(intake_job, repo)
        repo.complete_job(intake_job.id)
    except Exception as exc:
        record["stage_reached"] = "intake"
        record["error"] = str(exc)
        record["duration_s"] = round(time.time() - start, 3)
        return record

    if intake_result.get("terminal_decision") == Decision.REJECT.value:
        record["stage_reached"] = "intake"
        record["actual_decision"] = _decision_for_intake_reject()
        record["decision_match"] = record["actual_decision"] == record["expected_decision"]
        record["duration_s"] = round(time.time() - start, 3)
        return record

    try:
        review_job = repo.claim_next_job()
        if review_job is None:
            raise ValueError("missing_review_job")
        engine.handle_job(review_job, repo)
        repo.complete_job(review_job.id)
    except Exception as exc:
        record["stage_reached"] = "review"
        record["error"] = str(exc)
        record["duration_s"] = round(time.time() - start, 3)
        return record

    reviews = repo.children_of(submission.id, ObjectType.REVIEW)
    if not reviews:
        record["stage_reached"] = "review"
        record["error"] = "missing_review_object"
        record["duration_s"] = round(time.time() - start, 3)
        return record

    review = reviews[-1]
    review_metadata = review.metadata
    record["route"] = review_metadata.get("route", "single")
    record["cost_usd"] = float(review_metadata.get("cost_usd", 0.0) or 0.0)
    record["actual_rubric_scores"] = dict(review_metadata.get("rubric_scores", {}))
    record["actual_claim_support_verdict"] = review_metadata.get("claim_support_verdict")
    record["actual_overclaim_verdict"] = review_metadata.get("overclaim_verdict")
    record["actual_synthesis_quality_verdict"] = review_metadata.get("synthesis_quality_verdict")
    record["accept_blockers"] = _accept_blockers(review_metadata)
    record["rubric_score_deltas"] = {
        key: int(record["actual_rubric_scores"].get(key, 0) or 0) - int(entry.expected.rubric_scores.get(key, 0) or 0)
        for key in REVIEW_RUBRIC_KEYS
        if key in entry.expected.rubric_scores and key in record["actual_rubric_scores"]
    }

    try:
        editorial_job = repo.claim_next_job()
        if editorial_job is None:
            raise ValueError("missing_editorial_job")
        engine.handle_job(editorial_job, repo)
        repo.complete_job(editorial_job.id)
    except Exception as exc:
        record["stage_reached"] = "editorial"
        record["error"] = str(exc)
        record["duration_s"] = round(time.time() - start, 3)
        return record

    decisions = repo.children_of(submission.id, ObjectType.DECISION)
    if not decisions:
        record["stage_reached"] = "editorial"
        record["error"] = "missing_decision_object"
        record["duration_s"] = round(time.time() - start, 3)
        return record

    decision = str(decisions[-1].metadata.get("decision", "")).strip().lower()
    record["actual_decision"] = decision or None
    record["decision_match"] = record["actual_decision"] == record["expected_decision"]

    publish_job = repo.claim_next_job()
    if publish_job is not None:
        try:
            engine.handle_job(publish_job, repo)
            repo.complete_job(publish_job.id)
            record["stage_reached"] = "publish"
        except Exception as exc:
            record["stage_reached"] = "publish"
            record["error"] = str(exc)
            record["duration_s"] = round(time.time() - start, 3)
            return record
    else:
        record["stage_reached"] = "editorial"

    record["duration_s"] = round(time.time() - start, 3)
    return record


def summarize_gold_results(records: list[dict]) -> dict:
    confusion_matrix = _empty_confusion_matrix()
    correct = 0
    mismatches: list[dict] = []
    rubric_errors: dict[str, list[int]] = {key: [] for key in REVIEW_RUBRIC_KEYS}
    boolean_matches = {field: {"matched": 0, "count": 0} for field in BOOLEAN_FIELDS}
    by_article_type: dict[str, dict[str, int | float]] = {}
    accept_blockers: dict[str, int] = {}

    for record in records:
        expected = record.get("expected_decision")
        actual = record.get("actual_decision") or Decision.REJECT.value
        if expected in confusion_matrix and actual in confusion_matrix[expected]:
            confusion_matrix[expected][actual] += 1
        if actual == expected:
            correct += 1
        else:
            mismatches.append(
                {
                    "entry_id": record.get("entry_id"),
                    "title": record.get("title"),
                    "article_type": record.get("article_type"),
                    "expected": expected,
                    "actual": actual,
                    "accept_blockers": record.get("accept_blockers", []),
                    "error": record.get("error"),
                }
            )
        article_type = str(record.get("article_type", "unknown"))
        by_article_type.setdefault(article_type, {"count": 0, "correct": 0})
        by_article_type[article_type]["count"] += 1
        if actual == expected:
            by_article_type[article_type]["correct"] += 1

        expected_scores = record.get("expected_rubric_scores", {})
        actual_scores = record.get("actual_rubric_scores", {})
        for key in REVIEW_RUBRIC_KEYS:
            if key in expected_scores and key in actual_scores:
                rubric_errors[key].append(abs(int(actual_scores[key]) - int(expected_scores[key])))

        for field in BOOLEAN_FIELDS:
            expected_value = record.get(f"expected_{field}")
            actual_value = record.get(f"actual_{field}")
            if expected_value is None or actual_value is None:
                continue
            boolean_matches[field]["count"] += 1
            if expected_value == actual_value:
                boolean_matches[field]["matched"] += 1

        for blocker in record.get("accept_blockers", []):
            accept_blockers[blocker] = accept_blockers.get(blocker, 0) + 1

    total = len(records)
    for stats in by_article_type.values():
        count = int(stats["count"])
        stats["accuracy"] = round(int(stats["correct"]) / count, 3) if count else 0.0

    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "confusion_matrix": confusion_matrix,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "by_article_type": by_article_type,
        "rubric_mae": {
            key: round(sum(values) / len(values), 3) if values else None
            for key, values in rubric_errors.items()
        },
        "boolean_match_rates": {
            field: round(stats["matched"] / stats["count"], 3) if stats["count"] else None
            for field, stats in boolean_matches.items()
        },
        "accept_blockers": dict(sorted(accept_blockers.items(), key=lambda item: (-item[1], item[0]))),
    }


def evaluate_gold_set(corpus: GoldSetCorpus, engine: WorkflowEngine | None = None) -> dict:
    active_engine = engine or WorkflowEngine()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    records = [run_gold_entry(entry, active_engine) for entry in corpus.entries]
    return {
        "run_meta": {
            "timestamp": started_at,
            "entry_count": len(corpus.entries),
            "provider": getattr(active_engine.provider, "provider", active_engine.provider.__class__.__name__.lower()),
            "model": getattr(active_engine.provider, "model", "unknown"),
            "corpus_version": corpus.version,
        },
        "summary": summarize_gold_results(records),
        "results": records,
    }
