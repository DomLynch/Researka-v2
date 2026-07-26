from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from contracts.models import ArticleType, Decision

from .prompts import EDITOR_PROMPT_VERSION, REVIEWER_PROMPT_VERSION

JUDGE_POLICY_VERSION = "judge-policy-v1"
JUDGE_SETTINGS = {"accept_quorum_min": 2, "max_output_tokens": 3000, "response_format": "json_object"}
JUDGE_RELEASE_IDENTITY_KEYS = (
    "code_sha",
    "policy_version",
    "reviewer_prompt_version",
    "editor_prompt_version",
    "provider",
    "models",
    "settings",
)


def resolve_git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    env_sha = os.environ.get("RESEARKA_GIT_SHA", "").strip()
    if env_sha:
        return env_sha
    try:
        return Path("/etc/researka/git_sha").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


SERVICE_GIT_SHA = resolve_git_sha()


def resolve_judge_code_sha(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parents[1]
    paths = [
        *sorted((root / "runtime_core").glob("**/*.py")),
        *sorted((root / "contracts").glob("**/*.py")),
        root / "apps/runtime_api/app.py",
        root / "pyproject.toml",
        root / "uv.lock",
    ]
    digest = hashlib.sha256()
    included = 0
    for path in paths:
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        included += 1
    return f"sha256:{digest.hexdigest()}" if included else "unknown"


JUDGE_CODE_SHA = resolve_judge_code_sha()


def unsigned_calibration_sha256(raw: dict) -> str:
    unsigned = dict(raw)
    run_meta = dict(unsigned.get("run_meta", {}))
    run_meta.pop("human_signoff", None)
    unsigned["run_meta"] = run_meta
    payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def judge_release_id(raw: dict) -> str:
    identity = {key: raw.get(key) for key in JUDGE_RELEASE_IDENTITY_KEYS}
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


def judge_release_manifest_valid(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    return bool(
        str(raw.get("code_sha") or "").strip() not in {"", "unknown"}
        and all(str(raw.get(key) or "").strip() for key in JUDGE_RELEASE_IDENTITY_KEYS[1:5])
        and isinstance(raw.get("models"), list)
        and raw["models"]
        and all(str(model).strip() for model in raw["models"])
        and isinstance(raw.get("settings"), dict)
        and raw["settings"]
        and raw.get("id") == judge_release_id(raw)
    )


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _number_in_range(value: object, lower: float, upper: float) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    number = float(value)
    return math.isfinite(number) and lower <= number <= upper


def _bucket_total(value: object) -> int | None:
    if not isinstance(value, dict) or not all(
        isinstance(stats, dict)
        and _nonnegative_int(stats.get("count"))
        and _nonnegative_int(stats.get("correct"))
        and stats["correct"] <= stats["count"]
        and _number_in_range(stats.get("accuracy"), 0, 1)
        for stats in value.values()
    ):
        return None
    return sum(stats["count"] for stats in value.values())


def _confusion_total(value: object, labels: set[str]) -> int | None:
    if not isinstance(value, dict) or set(value) != labels:
        return None
    if not all(
        isinstance(value[label], dict)
        and set(value[label]) == labels
        and all(_nonnegative_int(count) for count in value[label].values())
        for label in labels
    ):
        return None
    return sum(sum(value[label].values()) for label in labels)


def _confusion_diagonal(value: object, labels: set[str]) -> int | None:
    if _confusion_total(value, labels) is None or not isinstance(value, dict):
        return None
    return sum(value[label][label] for label in labels)


def _class_metrics_valid(value: object, confusion: object, labels: set[str]) -> bool:
    if not isinstance(value, dict) or not isinstance(confusion, dict) or set(value) != labels:
        return False
    return all(
        isinstance(value[label], dict)
        and _nonnegative_int(value[label].get("count"))
        and value[label]["count"] == sum(confusion[label].values())
        and all(_number_in_range(value[label].get(key), 0, 1) for key in ("precision", "recall"))
        for label in labels
    )


def _derived_metrics_valid(summary: dict, results: list[dict]) -> bool:
    try:
        from .goldset import summarize_gold_results

        expected = summarize_gold_results(results)
    except (KeyError, TypeError, ValueError):
        return False
    return summary == expected


def _accuracy_valid(accuracy: object, correct: object, total: int) -> bool:
    if not isinstance(correct, int) or isinstance(correct, bool):
        return False
    if not isinstance(accuracy, (int, float)) or isinstance(accuracy, bool):
        return False
    return 0 <= correct <= total and _number_in_range(accuracy, 0, 1) and abs(float(accuracy) - correct / total) <= 0.001


def _mismatch_count_valid(value: object, correct: object, total: int) -> bool:
    return (
        isinstance(correct, int)
        and not isinstance(correct, bool)
        and _nonnegative_int(value)
        and value == total - correct
    )


def calibration_metrics_complete(raw: dict) -> bool:
    run_meta = raw.get("run_meta")
    summary = raw.get("summary")
    results = raw.get("results")
    if not isinstance(run_meta, dict) or not isinstance(summary, dict) or not isinstance(results, list):
        return False
    labels = {item.value for item in Decision}
    article_types = {item.value for item in ArticleType}
    confusion = summary.get("confusion_matrix")
    class_metrics = summary.get("class_metrics")
    by_type = summary.get("by_article_type")
    by_domain = summary.get("by_domain")
    cost = summary.get("cost")
    latency = summary.get("latency")
    total = len(results)
    confusion_total = _confusion_total(confusion, labels)
    type_total = _bucket_total(by_type)
    domain_total = _bucket_total(by_domain)
    agreement = run_meta.get("inter_adjudicator_decision_agreement")
    adjudicator_kappa = run_meta.get("inter_adjudicator_kappa")
    false_accept_rate = summary.get("false_accept_rate")
    judge_kappa = summary.get("cohen_kappa")
    correct = summary.get("correct")
    accuracy = summary.get("accuracy")
    diagonal = _confusion_diagonal(confusion, labels)
    return bool(
        total >= 100
        and all(
            isinstance(item, dict) and str(item.get("entry_id") or "").strip()
            for item in results
        )
        and len({str(item.get("entry_id")) for item in results if isinstance(item, dict)}) == total
        and summary.get("total") == total
        and confusion_total == total
        and correct == diagonal
        and _accuracy_valid(accuracy, correct, total)
        and _mismatch_count_valid(summary.get("mismatch_count"), correct, total)
        and _class_metrics_valid(class_metrics, confusion, labels)
        and isinstance(by_type, dict)
        and set(by_type) == article_types
        and type_total == total
        and isinstance(by_domain, dict)
        and len(by_domain) >= 8
        and domain_total == total
        and isinstance(cost, dict)
        and all(_number_in_range(cost.get(key), 0, math.inf) for key in ("total_usd", "mean_usd"))
        and isinstance(latency, dict)
        and all(_number_in_range(latency.get(key), 0, math.inf) for key in ("total_s", "mean_s", "p95_s"))
        and isinstance(summary.get("mismatches"), list)
        and summary.get("mismatch_count") == len(summary["mismatches"])
        and _number_in_range(false_accept_rate, 0, 1)
        and _number_in_range(judge_kappa, -1, 1)
        and _derived_metrics_valid(summary, results)
        and run_meta.get("judge_release_consistent") is True
        and run_meta.get("judge_release_target_matched") is True
        and _number_in_range(agreement, 0, 1)
        and _number_in_range(adjudicator_kappa, -1, 1)
    )


def calibration_timeline_valid(raw: dict) -> bool:
    run_meta = raw.get("run_meta")
    if not isinstance(run_meta, dict):
        return False
    try:
        frozen = datetime.fromisoformat(str(run_meta.get("labels_frozen_at")).replace("Z", "+00:00"))
        revealed = datetime.fromisoformat(str(run_meta.get("labels_revealed_at")).replace("Z", "+00:00"))
        evaluated = datetime.fromisoformat(str(run_meta.get("timestamp")).replace("Z", "+00:00"))
    except ValueError:
        return False
    if not frozen.tzinfo or not revealed.tzinfo or not evaluated.tzinfo:
        return False
    return frozen <= revealed <= evaluated <= datetime.now(timezone.utc)


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def build_judge_release(
    *,
    system_prompt: str,
    provider: str,
    model: str,
    response_metadata: dict,
) -> dict[str, object]:
    calibration_path = Path(
        os.environ.get(
            "RESEARKA_V2_CALIBRATION_PATH",
            str(Path(__file__).resolve().parents[1] / "artifacts" / "gold_set_eval_v3_current.json"),
        )
    )
    observed = response_metadata.get("panel_models") or response_metadata.get("accept_quorum_models")
    observed_models = sorted({str(item).strip() for item in observed if str(item).strip()}) if isinstance(observed, list) else []
    configured_models = sorted({item.strip() for item in model.split("|") if item.strip()})
    manifest: dict[str, object] = {
        "code_sha": JUDGE_CODE_SHA,
        "source_commit": SERVICE_GIT_SHA,
        "policy_version": JUDGE_POLICY_VERSION,
        "reviewer_prompt_version": REVIEWER_PROMPT_VERSION,
        "editor_prompt_version": EDITOR_PROMPT_VERSION,
        "request_prompt_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
        "provider": provider,
        "models": configured_models or [model],
        "observed_models": observed_models,
        "settings": dict(JUDGE_SETTINGS),
        "calibration": {
            "artifact": calibration_path.name,
            "sha256": _file_sha256(calibration_path),
        },
    }
    return {"id": judge_release_id(manifest), **manifest}
