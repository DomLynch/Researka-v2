from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import timezone
from pathlib import Path
from typing import Any

from contracts import ObjectType, ResearchObject

ACTOR_ID = "researka:v2"


def _enabled() -> bool:
    value = os.getenv("RESEARKA_DW_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _key() -> str | None:
    configured = os.getenv("RESEARKA_DW_API_KEY", "").strip()
    if configured:
        return configured
    path = Path(os.getenv("RESEARKA_DW_KEY_FILE", "/etc/derivation-web/researka.key"))
    if not path.exists():
        return None
    return path.read_text().strip()


def _url(path: str) -> str:
    base = os.getenv("RESEARKA_DW_URL", "https://dw.domlynch.com").rstrip("/")
    return f"{base}{path}"


def _post(path: str, payload: dict[str, Any], *, api_key: str) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        _url(path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(os.getenv("RESEARKA_DW_TIMEOUT_SEC", "5"))) as response:
            body = response.read().decode("utf-8")
            return int(response.status), json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return exc.code, {}
        raise


def _artifact_body(obj: ResearchObject) -> str:
    body = obj.body_markdown or obj.metadata.get("abstract") or ""
    return str(body).strip() or obj.title


def emit_decision_to_derivation_web(
    *,
    submission: ResearchObject,
    decision: ResearchObject,
    review: ResearchObject | None = None,
) -> dict[str, Any]:
    if not _enabled():
        return {"ok": False, "skipped": "disabled"}
    api_key = _key()
    if not api_key:
        return {"ok": False, "skipped": "missing_key"}

    try:
        _post("/api/actors", {"id": ACTOR_ID, "kind": "agent", "name": "Researka v2"}, api_key=api_key)
        submission_status, submission_artifact = _post(
            "/api/artifacts",
            {
                "kind": "source",
                "content_type": "text/markdown",
                "body_text": _artifact_body(submission),
                "metadata": {
                    "researka_object_type": ObjectType.SUBMISSION.value,
                    "researka_submission_id": submission.id,
                    "title": submission.title,
                    "domain_slug": submission.metadata.get("domain_slug"),
                    "article_type": submission.metadata.get("article_type"),
                },
                "actor_id": ACTOR_ID,
            },
            api_key=api_key,
        )
        decision_status, decision_artifact = _post(
            "/api/artifacts",
            {
                "kind": "claim",
                "content_type": "application/json",
                "body_text": json.dumps(
                    {
                        "decision": decision.metadata.get("decision"),
                        "notes": decision.metadata.get("notes", []),
                        "gate_failures": decision.metadata.get("gate_failures", []),
                        "review_recommendation": (review.metadata.get("recommendation") if review else None),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "metadata": {
                    "researka_object_type": ObjectType.DECISION.value,
                    "researka_submission_id": submission.id,
                    "researka_decision_id": decision.id,
                    "researka_review_id": review.id if review else decision.metadata.get("review_id"),
                    "provider": decision.metadata.get("provider"),
                    "model": decision.metadata.get("model"),
                    "prompt_version": decision.metadata.get("prompt_version"),
                },
                "actor_id": ACTOR_ID,
            },
            api_key=api_key,
        )
        submission_artifact_id = submission_artifact.get("id")
        decision_artifact_id = decision_artifact.get("id")
        if submission_artifact_id and decision_artifact_id:
            _post(
                "/api/steps",
                {
                    "step_type": "classify",
                    "input_artifact_ids": [submission_artifact_id],
                    "output_artifact_id": decision_artifact_id,
                    "actor_id": ACTOR_ID,
                    "method": {
                        "system": "researka-v2",
                        "stage": "autonomous_editorial_decision",
                        "decision": decision.metadata.get("decision"),
                    },
                    "created_at": decision.created_at.astimezone(timezone.utc).isoformat(),
                },
                api_key=api_key,
            )
        return {
            "ok": True,
            "submission_artifact_id": submission_artifact_id,
            "decision_artifact_id": decision_artifact_id,
            "submission_status": submission_status,
            "decision_status": decision_status,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:240]}
