from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

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


def _cohen_kappa(confusion_matrix: dict[str, dict[str, int]]) -> float | None:
    total = sum(sum(row.values()) for row in confusion_matrix.values())
    if not total:
        return None
    observed = sum(confusion_matrix[label][label] for label in DECISION_LABELS) / total
    expected = sum(
        sum(confusion_matrix[label].values())
        * sum(confusion_matrix[row][label] for row in DECISION_LABELS)
        for label in DECISION_LABELS
    ) / (total * total)
    if expected == 1:
        return 1.0 if observed == 1 else None
    return round((observed - expected) / (1 - expected), 3)


def _class_metrics(confusion_matrix: dict[str, dict[str, int]]) -> dict[str, dict[str, float | int]]:
    metrics: dict[str, dict[str, float | int]] = {}
    for label in DECISION_LABELS:
        true_positive = confusion_matrix[label][label]
        predicted = sum(confusion_matrix[row][label] for row in DECISION_LABELS)
        expected = sum(confusion_matrix[label].values())
        metrics[label] = {
            "count": expected,
            "precision": round(true_positive / predicted, 3) if predicted else 0.0,
            "recall": round(true_positive / expected, 3) if expected else 0.0,
        }
    return metrics


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

    record: dict[str, Any] = {
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
        "actual_review_summary": "",
        "actual_required_revisions": [],
        "actual_major_issues": [],
        "judge_release_id": None,
        "judge_release": None,
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
    record["actual_review_summary"] = review.body_markdown
    record["actual_required_revisions"] = list(review_metadata.get("required_revisions") or [])
    record["actual_major_issues"] = list(review_metadata.get("major_issues") or [])
    record["judge_release_id"] = review_metadata.get("judge_release_id")
    record["judge_release"] = review_metadata.get("judge_release")
    record["accept_blockers"] = _accept_blockers(review_metadata)
    actual_rubric_scores = record["actual_rubric_scores"]
    record["rubric_score_deltas"] = {
        key: int(actual_rubric_scores.get(key, 0) or 0) - int(entry.expected.rubric_scores.get(key, 0) or 0)
        for key in REVIEW_RUBRIC_KEYS
        if key in entry.expected.rubric_scores and key in actual_rubric_scores
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
    by_domain: dict[str, dict[str, int | float]] = {}
    accept_blockers: dict[str, int] = {}

    for record in records:
        expected = record.get("expected_decision")
        actual = record.get("actual_decision")
        if expected in confusion_matrix and actual in confusion_matrix[expected]:
            confusion_matrix[expected][actual] += 1
        if actual == expected:
            correct += 1
        else:
            reasons = (
                record.get("actual_required_revisions")
                or record.get("actual_major_issues")
                or ([record["error"]] if record.get("error") else [])
                or ([record["actual_review_summary"]] if record.get("actual_review_summary") else [])
            )
            mismatches.append(
                {
                    "entry_id": record.get("entry_id"),
                    "title": record.get("title"),
                    "article_type": record.get("article_type"),
                    "expected": expected,
                    "actual": actual,
                    "accept_blockers": record.get("accept_blockers", []),
                    "error": record.get("error"),
                    "reason": "; ".join(str(reason).strip() for reason in reasons if str(reason).strip()),
                }
            )
        article_type = str(record.get("article_type", "unknown"))
        by_article_type.setdefault(article_type, {"count": 0, "correct": 0})
        by_article_type[article_type]["count"] += 1
        if actual == expected:
            by_article_type[article_type]["correct"] += 1
        domain = str(record.get("domain_slug", "general"))
        by_domain.setdefault(domain, {"count": 0, "correct": 0})
        by_domain[domain]["count"] += 1
        if actual == expected:
            by_domain[domain]["correct"] += 1

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
    for stats in by_domain.values():
        count = int(stats["count"])
        stats["accuracy"] = round(int(stats["correct"]) / count, 3) if count else 0.0
    non_accept_count = sum(sum(confusion_matrix[label].values()) for label in DECISION_LABELS if label != "accept")
    false_accepts = sum(confusion_matrix[label]["accept"] for label in DECISION_LABELS if label != "accept")
    durations = sorted(float(record.get("duration_s", 0.0) or 0.0) for record in records)
    costs = [float(record.get("cost_usd", 0.0) or 0.0) for record in records]

    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "confusion_matrix": confusion_matrix,
        "class_metrics": _class_metrics(confusion_matrix),
        "cohen_kappa": _cohen_kappa(confusion_matrix),
        "false_accept_count": false_accepts,
        "false_accept_rate": round(false_accepts / non_accept_count, 3) if non_accept_count else 0.0,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "by_article_type": by_article_type,
        "by_domain": by_domain,
        "cost": {
            "total_usd": round(sum(costs), 4),
            "mean_usd": round(sum(costs) / total, 4) if total else 0.0,
        },
        "latency": {
            "total_s": round(sum(durations), 3),
            "mean_s": round(sum(durations) / total, 3) if total else 0.0,
            "p95_s": durations[max(0, (95 * len(durations) + 99) // 100 - 1)] if durations else 0.0,
        },
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


def evaluate_gold_set(
    corpus: GoldSetCorpus,
    engine: WorkflowEngine | None = None,
    progress_callback: Callable[[int, int, dict, dict], None] | None = None,
) -> dict:
    active_engine = engine or WorkflowEngine()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    total = len(corpus.entries)
    records: list[dict] = []
    artifact: dict[str, Any] = {
        "run_meta": {
            "timestamp": started_at,
            "entry_count": total,
            "provider": getattr(active_engine.provider, "provider", active_engine.provider.__class__.__name__.lower()),
            "model": getattr(active_engine.provider, "model", "unknown"),
            "corpus_version": corpus.version,
            **corpus.adjudication.model_dump(mode="json", exclude_none=True),
        },
        "summary": summarize_gold_results(records),
        "results": records,
        "limitations": [
            "Performance is specific to the frozen submission sample, article types, domains, and judge release.",
            "Adjudicated labels are review judgments, not proof that every scientific conclusion is objectively true.",
            "Provider nondeterminism may change repeated-run latency, cost, and borderline decisions.",
        ],
    }
    for index, entry in enumerate(corpus.entries, start=1):
        record = run_gold_entry(entry, active_engine)
        records.append(record)
        artifact["summary"] = summarize_gold_results(records)
        release_records = [
            item
            for item in records
            if item.get("judge_release_id") and isinstance(item.get("judge_release"), dict)
        ]
        releases = {
            str(item["judge_release_id"]): item["judge_release"]
            for item in release_records
        }
        release_consistent = len(release_records) == len(records) and len(releases) == 1
        artifact["run_meta"]["judge_release_consistent"] = release_consistent
        if release_consistent:
            release_id, release = next(iter(releases.items()))
            artifact["run_meta"]["judge_release_id"] = release_id
            artifact["run_meta"]["judge_release"] = release
            artifact["run_meta"]["judge_release_target_matched"] = (
                release_id == artifact["run_meta"].get("target_judge_release_id")
            )
        else:
            artifact["run_meta"].pop("judge_release_id", None)
            artifact["run_meta"].pop("judge_release", None)
            artifact["run_meta"]["judge_release_target_matched"] = False
        if progress_callback is not None:
            progress_callback(index, total, record, artifact)
    return artifact


def render_gold_set_report(artifact: dict) -> str:
    summary = artifact.get("summary", {})
    run_meta = artifact.get("run_meta", {})
    lines = [
        "# Gold Set Evaluation Report",
        "",
        f"- Timestamp: `{run_meta.get('timestamp', 'unknown')}`",
        f"- Corpus version: `{run_meta.get('corpus_version', 'unknown')}`",
        f"- Provider: `{run_meta.get('provider', 'unknown')}`",
        f"- Model: `{run_meta.get('model', 'unknown')}`",
        f"- Accuracy: `{summary.get('correct', 0)}` / `{summary.get('total', 0)}` (`{summary.get('accuracy', 0.0):.1%}`)",
        f"- Mismatches: `{summary.get('mismatch_count', 0)}`",
        "",
        "## By article type",
        "",
        "| Article type | Count | Correct | Accuracy |",
        "|---|---:|---:|---:|",
    ]
    for article_type, stats in sorted(summary.get("by_article_type", {}).items()):
        lines.append(
            f"| {article_type} | {stats.get('count', 0)} | {stats.get('correct', 0)} | {stats.get('accuracy', 0.0):.1%} |"
        )

    lines.extend(
        [
            "",
            "## Accept blockers",
            "",
            "| Blocker | Count |",
            "|---|---:|",
        ]
    )
    accept_blockers = summary.get("accept_blockers", {})
    if accept_blockers:
        for blocker, count in accept_blockers.items():
            lines.append(f"| {blocker} | {count} |")
    else:
        lines.append("| none | 0 |")

    lines.extend(
        [
            "",
            "## Mismatches",
            "",
            "| Entry | Article type | Expected | Actual | Reason |",
            "|---|---|---|---|---|",
        ]
    )
    mismatches = summary.get("mismatches", [])
    if mismatches:
        for mismatch in mismatches:
            lines.append(
                "| {entry_id} | {article_type} | {expected} | {actual} | {error} |".format(
                    entry_id=mismatch.get("entry_id", "unknown"),
                    article_type=mismatch.get("article_type", "unknown"),
                    expected=mismatch.get("expected", "unknown"),
                    actual=mismatch.get("actual", "unknown"),
                    error=(mismatch.get("reason") or mismatch.get("error") or "").replace("\n", " "),
                )
            )
    else:
        lines.append("| none | - | - | - | - |")

    return "\n".join(lines) + "\n"
