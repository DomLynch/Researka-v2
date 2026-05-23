from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from contracts import Decision, ObjectType, ResearchObject
from runtime_core.publication_sidecars import build_sidecar, screening_summary, sidecar_manifest

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


def is_configured() -> bool:
    return _enabled() and _key() is not None


def _url(path: str) -> str:
    base = _base_url()
    return f"{base}{path}"


def _base_url() -> str:
    return os.getenv("RESEARKA_DW_URL", "https://provenance.researka.org").rstrip("/")


def _public_api_base_url() -> str:
    return os.getenv("RESEARKA_PUBLIC_API_BASE_URL", "https://api.researka.org").rstrip("/")


def _absolute_sidecar_url(path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
    return f"{_public_api_base_url()}{path}"


def _dw_sidecar_manifest(publication_id: str) -> list[dict[str, str]]:
    return [
        {**sidecar, "url": _absolute_sidecar_url(sidecar["url"])}
        for sidecar in sidecar_manifest(publication_id)
    ]


def _post(path: str, payload: dict[str, Any], *, api_key: str) -> tuple[int, dict[str, Any]]:
    attempts = max(1, int(os.getenv("RESEARKA_DW_POST_ATTEMPTS", "4")))
    for attempt in range(attempts):
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
            if exc.code == 429 and attempt < attempts - 1:
                retry_after = exc.headers.get("Retry-After")
                delay = float(str(retry_after or os.getenv("RESEARKA_DW_RETRY_DELAY_SEC", "2")))
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("dw_post_retry_exhausted")


def _artifact_body(obj: ResearchObject) -> str:
    body = obj.body_markdown or obj.metadata.get("abstract") or ""
    return str(body).strip() or obj.title


def _bool_meta(obj: ResearchObject | None, key: str) -> bool:
    """Read a boolean from the review metadata, defaulting to False."""
    if obj is None:
        return False
    return bool(obj.metadata.get(key, False))


def _str_meta(obj: ResearchObject | None, key: str) -> str | None:
    """Read a non-empty string from the review metadata, returning None if absent."""
    if obj is None:
        return None
    value = obj.metadata.get(key)
    if value in (None, ""):
        return None
    return str(value)


def _fallback_metadata(review: ResearchObject | None) -> dict[str, Any]:
    return {
        "primary_fallback_used": _bool_meta(review, "primary_fallback_used"),
        "sparring_fallback_used": _bool_meta(review, "sparring_fallback_used"),
        "primary_fallback_reason": _str_meta(review, "primary_fallback_reason"),
        "sparring_fallback_reason": _str_meta(review, "sparring_fallback_reason"),
        "panel_route": _str_meta(review, "route"),
    }


def _source_metadata(submission: ResearchObject) -> dict[str, Any]:
    return {
        "researka_object_type": ObjectType.SUBMISSION.value,
        "researka_submission_id": submission.id,
        "title": submission.title,
        "domain_slug": submission.metadata.get("domain_slug"),
        "article_type": submission.metadata.get("article_type"),
    }


def _emit_claim_chain(
    *,
    submission: ResearchObject,
    claim_body: str,
    claim_content_type: str,
    claim_metadata: dict[str, Any],
    stage: str,
    decision_value: Any,
    created_at: datetime,
    api_key: str,
    extra_input_payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    _post("/api/actors", {"id": ACTOR_ID, "kind": "agent", "name": "Researka v2"}, api_key=api_key)
    source_status, source_artifact = _post(
        "/api/artifacts",
        {
            "kind": "source",
            "content_type": "text/markdown",
            "body_text": _artifact_body(submission),
            "metadata": _source_metadata(submission),
            "actor_id": ACTOR_ID,
        },
        api_key=api_key,
    )
    claim_status, claim_artifact = _post(
        "/api/artifacts",
        {
            "kind": "claim",
            "content_type": claim_content_type,
            "body_text": claim_body,
            "metadata": claim_metadata,
            "actor_id": ACTOR_ID,
        },
        api_key=api_key,
    )
    source_id = source_artifact.get("id")
    claim_id = claim_artifact.get("id")
    extra_artifacts: list[dict[str, Any]] = []
    for payload in extra_input_payloads or []:
        _, artifact = _post("/api/artifacts", payload, api_key=api_key)
        if artifact.get("id"):
            extra_artifacts.append(artifact)
    step: dict[str, Any] = {}
    if source_id and claim_id:
        _, step = _post(
            "/api/steps",
            {
                "step_type": "classify",
                "input_artifact_ids": [source_id, *[artifact["id"] for artifact in extra_artifacts]],
                "output_artifact_id": claim_id,
                "actor_id": ACTOR_ID,
                "method": {"system": "researka-v2", "stage": stage, "decision": decision_value},
                "created_at": created_at.astimezone(timezone.utc).isoformat(),
            },
            api_key=api_key,
        )
    return {
        "source_status": source_status,
        "claim_status": claim_status,
        "source_artifact": source_artifact,
        "claim_artifact": claim_artifact,
        "extra_artifacts": extra_artifacts,
        "step": step,
    }


def _publication_dw_inputs(
    *,
    submission: ResearchObject,
    publication: ResearchObject,
    review: ResearchObject | None,
    decision: ResearchObject | None,
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    for sidecar in _dw_sidecar_manifest(publication.id):
        try:
            payload, media_type, filename = build_sidecar(publication, submission, sidecar["name"])
        except KeyError:
            continue
        body_text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True)
        inputs.append(
            {
                "kind": "source",
                "content_type": media_type,
                "body_text": body_text,
                "metadata": {
                    "researka_object_type": "publication_sidecar",
                    "researka_publication_id": publication.id,
                    "researka_submission_id": submission.id,
                    "sidecar_name": filename,
                    "sidecar_url": sidecar["url"],
                },
                "actor_id": ACTOR_ID,
            }
        )
    if decision is not None:
        inputs.append(
            {
                "kind": "source",
                "content_type": "application/json",
                "body_text": json.dumps(
                    {
                        "decision": decision.metadata.get("decision"),
                        "notes": decision.metadata.get("notes", []),
                        "gate_failures": decision.metadata.get("gate_failures", []),
                        "review_recommendation": review.metadata.get("recommendation") if review else None,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "metadata": {
                    "researka_object_type": "publication_decision_trace",
                    "researka_publication_id": publication.id,
                    "researka_submission_id": submission.id,
                    "researka_decision_id": decision.id,
                    "researka_review_id": review.id if review else decision.metadata.get("review_id"),
                },
                "actor_id": ACTOR_ID,
            }
        )
    return inputs


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
        chain = _emit_claim_chain(
            submission=submission,
            claim_content_type="application/json",
            claim_body=json.dumps(
                {
                    "decision": decision.metadata.get("decision"),
                    "notes": decision.metadata.get("notes", []),
                    "gate_failures": decision.metadata.get("gate_failures", []),
                    "review_recommendation": (review.metadata.get("recommendation") if review else None),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            claim_metadata={
                "researka_object_type": ObjectType.DECISION.value,
                "researka_submission_id": submission.id,
                "researka_decision_id": decision.id,
                "researka_review_id": review.id if review else decision.metadata.get("review_id"),
                "provider": decision.metadata.get("provider"),
                "model": decision.metadata.get("model"),
                "prompt_version": decision.metadata.get("prompt_version"),
                **_fallback_metadata(review),
            },
            stage="autonomous_editorial_decision",
            decision_value=decision.metadata.get("decision"),
            created_at=decision.created_at,
            api_key=api_key,
        )
        return {
            "ok": True,
            "submission_artifact_id": chain["source_artifact"].get("id"),
            "decision_artifact_id": chain["claim_artifact"].get("id"),
            "submission_status": chain["source_status"],
            "decision_status": chain["claim_status"],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:240]}


def emit_publication_to_derivation_web(
    *,
    submission: ResearchObject,
    publication: ResearchObject,
    review: ResearchObject | None = None,
    decision: ResearchObject | None = None,
) -> dict[str, Any]:
    if not _enabled() or not (api_key := _key()):
        return {}

    try:
        decision_value = decision.metadata.get("decision") if decision else Decision.ACCEPT.value
        chain = _emit_claim_chain(
            submission=submission,
            claim_content_type="text/markdown",
            claim_body=publication.body_markdown,
            claim_metadata={
                "provenance_schema_version": "publication_sidecars_v1",
                "researka_object_type": ObjectType.PUBLICATION.value,
                "researka_publication_id": publication.id,
                "researka_submission_id": submission.id,
                "researka_review_id": review.id if review else None,
                "researka_decision_id": decision.id if decision else None,
                "title": publication.title,
                "domain_slug": submission.metadata.get("domain_slug"),
                "article_type": publication.metadata.get("article_type"),
                "author_agent_id": publication.metadata.get("author_agent_id"),
                "decision": decision_value,
                "doi": publication.metadata.get("doi"),
                "doi_status": publication.metadata.get("doi_status"),
                "osf_url": publication.metadata.get("osf_url"),
                "prompt_version": publication.metadata.get("prompt_version"),
                "screening": screening_summary(publication),
                "sidecars": _dw_sidecar_manifest(publication.id),
                **_fallback_metadata(review),
            },
            stage="autonomous_publish",
            decision_value=decision_value,
            created_at=publication.created_at,
            api_key=api_key,
            extra_input_payloads=_publication_dw_inputs(
                submission=submission,
                publication=publication,
                review=review,
                decision=decision,
            ),
        )
        artifact_id = chain["claim_artifact"].get("id")
        if not artifact_id:
            return {"dw_status": "failed", "dw_error": "missing_publication_artifact_id"}
        metadata = {
            "dw_artifact_id": str(artifact_id),
            "dw_chain_url": f"{_base_url()}/artifacts/{artifact_id}/chain",
            "dw_api_chain_url": f"{_base_url()}/api/artifacts/{artifact_id}/chain",
            "dw_source_artifact_id": chain["source_artifact"].get("id"),
            "dw_input_artifact_ids": [artifact.get("id") for artifact in chain["extra_artifacts"] if artifact.get("id")],
            "dw_step_id": chain["step"].get("id"),
            "dw_step_hash": chain["step"].get("step_hash"),
            "dw_status": "registered",
        }
        if content_hash := chain["claim_artifact"].get("content_hash"):
            metadata["content_hash"] = f"sha256:{content_hash}"
            metadata["sha256"] = f"sha256:{content_hash}"
        return metadata
    except Exception as exc:
        return {"dw_status": "failed", "dw_error": str(exc)[:240]}


def _latest_accept_decision(repository: Any, submission_id: str) -> ResearchObject | None:
    decisions = [
        obj
        for obj in repository.children_of(submission_id, ObjectType.DECISION)
        if obj.metadata.get("decision") == Decision.ACCEPT.value
    ]
    return decisions[-1] if decisions else None


def _review_for_decision(repository: Any, decision: ResearchObject | None) -> ResearchObject | None:
    if decision is None or not decision.metadata.get("review_id"):
        return None
    return repository.get_object(str(decision.metadata["review_id"]))


def backfill_missing_publication_chains(
    repository: Any,
    *,
    apply: bool = False,
    limit: int | None = None,
    publication_id: str | None = None,
    refresh_existing: bool = False,
    emit_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Backfill DW metadata for existing publications.

    Dry-run never calls Derivation Web. Apply reuses the same emit path as new
    publications and persists only returned DW metadata.
    """
    emit = emit_fn or emit_publication_to_derivation_web
    summary: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "scanned": 0,
        "eligible": 0,
        "registered": 0,
        "already_registered": 0,
        "skipped": 0,
        "failed": 0,
        "items": [],
    }

    publications = repository.list_objects(ObjectType.PUBLICATION)
    if publication_id is not None:
        publications = [pub for pub in publications if pub.id == publication_id]

    for publication in publications:
        if limit is not None and summary["eligible"] >= limit:
            break
        summary["scanned"] += 1
        item: dict[str, Any] = {
            "publication_id": publication.id,
            "title": publication.title,
            "status": "",
        }
        if publication.metadata.get("dw_artifact_id") and not refresh_existing:
            summary["already_registered"] += 1
            item["status"] = "already_registered"
            summary["items"].append(item)
            continue

        submission_id = publication.parent_object_id
        if not submission_id:
            summary["skipped"] += 1
            item["status"] = "missing_parent_submission"
            summary["items"].append(item)
            continue
        submission = repository.get_object(submission_id)
        if submission is None:
            summary["skipped"] += 1
            item["status"] = "missing_parent_submission"
            item["submission_id"] = submission_id
            summary["items"].append(item)
            continue

        summary["eligible"] += 1
        item["submission_id"] = submission.id
        item["author_agent_id"] = submission.metadata.get("author_agent_id")
        decision = _latest_accept_decision(repository, submission.id)
        review = _review_for_decision(repository, decision)
        if not apply:
            item["status"] = "would_refresh" if publication.metadata.get("dw_artifact_id") else "would_register"
            summary["items"].append(item)
            continue

        try:
            emitted = emit(submission=submission, publication=publication, review=review, decision=decision)
            if not emitted:
                summary["skipped"] += 1
                item["status"] = "dw_not_configured"
                summary["items"].append(item)
                continue
            if emitted.get("dw_status") == "failed":
                raise RuntimeError(str(emitted.get("dw_error", ""))[:240])
            metadata = {**publication.metadata, **emitted}
            updated = repository.update_object_metadata(publication.id, metadata)
            if updated is None:
                raise RuntimeError("publication_disappeared")
        except Exception as exc:
            summary["failed"] += 1
            item["status"] = "failed"
            item["error"] = str(exc)[:240]
            summary["items"].append(item)
            continue
        summary["registered"] += 1
        item.update(
            {
                "status": "registered",
                "dw_artifact_id": emitted.get("dw_artifact_id"),
                "dw_chain_url": emitted.get("dw_chain_url"),
                "sha256": emitted.get("sha256"),
            }
        )
        summary["items"].append(item)

    return summary
