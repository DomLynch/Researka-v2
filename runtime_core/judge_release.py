from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from .prompts import EDITOR_PROMPT_VERSION, REVIEWER_PROMPT_VERSION

JUDGE_POLICY_VERSION = "judge-policy-v1"
JUDGE_SETTINGS = {"accept_quorum_min": 2, "max_output_tokens": 3000, "response_format": "json_object"}


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
    models = response_metadata.get("panel_models") or response_metadata.get("accept_quorum_models")
    model_list = sorted({str(item).strip() for item in models if str(item).strip()}) if isinstance(models, list) else []
    manifest: dict[str, object] = {
        "code_sha": SERVICE_GIT_SHA,
        "policy_version": JUDGE_POLICY_VERSION,
        "reviewer_prompt_version": REVIEWER_PROMPT_VERSION,
        "editor_prompt_version": EDITOR_PROMPT_VERSION,
        "reviewer_prompt_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
        "provider": provider,
        "models": model_list or [model],
        "settings": dict(JUDGE_SETTINGS),
        "calibration": {
            "artifact": calibration_path.name,
            "sha256": _file_sha256(calibration_path),
        },
    }
    release_id = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"id": f"sha256:{release_id}", **manifest}
