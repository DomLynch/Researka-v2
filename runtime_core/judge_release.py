from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess  # nosec B404 - used only for fixed git metadata command
from datetime import datetime, timezone
from pathlib import Path

from contracts.models import ArticleType, Decision

from .prompts import EDITOR_PROMPT_VERSION, REVIEWER_PROMPT_VERSION

JUDGE_POLICY_VERSION = "judge-policy-v2"
JUDGE_SETTINGS = {"accept_quorum_min": 2, "max_output_tokens": 3000, "response_format": "json_object"}
JUDGE_RELEASE_IDENTITY_KEYS = (
    "code_sha",
    "policy_version",
    "reviewer_prompt_version",
    "editor_prompt_version",
    "provider",
    "models",
    "observed_models",
    "settings",
    "request_prompt_sha256",
    "calibration",
)


def resolve_git_sha() -> str:
    root = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(  # nosec B603 B607 - fixed executable and arguments
            ["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"],
            cwd=root,
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
        and isinstance(raw.get("observed_models"), list)
        and raw["observed_models"]
        and all(str(model).strip() for model in raw["observed_models"])
        and isinstance(raw.get("settings"), dict)
        and raw["settings"]
        and re.fullmatch(r"[0-9a-f]{64}", str(raw.get("request_prompt_sha256") or "")) is not None
        and isinstance(raw.get("calibration"), dict)
        and bool(raw["calibration"].get("artifact"))
        and re.fullmatch(r"[0-9a-f]{64}", str(raw["calibration"].get("sha256") or "")) is not None
        and raw.get("id") == judge_release_id(raw)
    )


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _number_in_range(value: object, lower: float, upper: float) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    number = float(value)
    return math.isfinite(number) and lower <= number <= upper


def _derived_metrics_valid(summary: dict, results: list[dict]) -> bool:
    try:
        from .goldset import summarize_gold_results

        expected = summarize_gold_results(results)
    except (KeyError, TypeError, ValueError):
        return False
    return summary == expected


def _calibration_results_valid(results: list[object]) -> bool:
    labels = {item.value for item in Decision}
    article_types = {item.value for item in ArticleType}
    rows = [item for item in results if isinstance(item, dict)]
    return bool(
        len(rows) == len(results) >= 100
        and all(
            str(row.get("entry_id") or "").strip()
            and row.get("expected_decision") in labels
            and row.get("actual_decision") in labels
            and row.get("article_type") in article_types
            and str(row.get("domain_slug") or "").strip()
            and _number_in_range(row.get("cost_usd"), 0, math.inf)
            and _number_in_range(row.get("duration_s"), 0, math.inf)
            for row in rows
        )
        and len({str(row["entry_id"]) for row in rows}) == len(rows)
        and {str(row["article_type"]) for row in rows} == article_types
        and len({str(row["domain_slug"]) for row in rows}) >= 8
    )


def calibration_metrics_complete(raw: dict) -> bool:
    run_meta = raw.get("run_meta")
    summary = raw.get("summary")
    results = raw.get("results")
    if not isinstance(run_meta, dict) or not isinstance(summary, dict) or not isinstance(results, list):
        return False
    agreement = run_meta.get("inter_adjudicator_decision_agreement")
    adjudicator_kappa = run_meta.get("inter_adjudicator_kappa")
    return bool(
        _calibration_results_valid(results)
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
    root = Path(__file__).resolve().parents[1]
    calibration_path = Path(
        os.environ.get(
            "RESEARKA_V2_CALIBRATION_CORPUS_PATH",
            os.environ.get(
                "RESEARKA_V2_CALIBRATION_PATH",
                str(root / "calibration" / "real_gold_set_v1_freeze_receipt.json"),
            ),
        )
    )
    observed = response_metadata.get("panel_models") or response_metadata.get("accept_quorum_models")
    observed_models = sorted({str(item).strip() for item in observed if str(item).strip()}) if isinstance(observed, list) else []
    configured_models = sorted({item.strip() for item in model.split("|") if item.strip()})
    settings = dict(JUDGE_SETTINGS)
    if response_metadata.get("accept_quorum_waiver_verified") is True:
        settings.update(
            accept_quorum_min=1,
            accept_quorum_waiver="sparring_billing_unavailable",
        )
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
        "settings": settings,
        "calibration": {
            "artifact": calibration_path.name,
            "sha256": _file_sha256(calibration_path),
        },
    }
    return {"id": judge_release_id(manifest), **manifest}
