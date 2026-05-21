from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib import error, request

from contracts import Decision, ObjectType, ResearchObject


SYSTEM_ACTOR_ID = "researka:v2"


@dataclass(frozen=True)
class DerivationWebConfig:
    base_url: str
    api_key: str
    timeout_seconds: float = 10.0


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def _read_api_key() -> str | None:
    direct = os.environ.get("RESEARKA_V2_DW_API_KEY")
    if direct and direct.strip():
        return direct.strip()
    key_path = os.environ.get("RESEARKA_V2_DW_API_KEY_PATH")
    if key_path and key_path.strip():
        path = Path(key_path.strip())
        if path.exists():
            return path.read_text().strip()
    return None


def config_from_env() -> DerivationWebConfig | None:
    enabled = os.environ.get("RESEARKA_V2_DW_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no"}:
        return None
    api_key = _read_api_key()
    if not api_key:
        return None
    base_url = os.environ.get("RESEARKA_V2_DW_BASE_URL", "https://provenance.researka.org").rstrip("/")
    timeout = float(os.environ.get("RESEARKA_V2_DW_TIMEOUT_SECONDS", "10"))
    return DerivationWebConfig(base_url=base_url, api_key=api_key, timeout_seconds=timeout)


class DerivationWebClient:
    def __init__(self, config: DerivationWebConfig) -> None:
        self.config = config

    def _post(self, path: str, payload: dict[str, Any], *, ok_statuses: set[int] | None = None) -> dict[str, Any] | None:
        ok_statuses = ok_statuses or {200, 201}
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        req = request.Request(
            f"{self.config.base_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                status = response.status
                data = response.read().decode("utf-8")
        except error.HTTPError as exc:
            if exc.code in ok_statuses:
                return None
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"dw_post_failed:{path}:{exc.code}:{detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"dw_unreachable:{path}:{exc.reason}") from exc

        if status not in ok_statuses:
            raise RuntimeError(f"dw_post_failed:{path}:{status}:{data}")
        if not data:
            return None
        parsed = json.loads(data)
        return parsed if isinstance(parsed, dict) else None

    def ensure_actor(self, actor_id: str, *, kind: str, name: str) -> None:
        self._post(
            "/api/actors",
            {"id": actor_id, "kind": kind, "name": name},
            ok_statuses={200, 201, 409},
        )

    def create_artifact(
        self,
        *,
        kind: str,
        content_type: str,
        body_text: str,
        metadata: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        response = self._post(
            "/api/artifacts",
            {
                "kind": kind,
                "content_type": content_type,
                "body_text": body_text,
                "metadata": metadata,
                "actor_id": actor_id,
            },
        )
        if not response:
            raise RuntimeError("dw_artifact_empty_response")
        return response

    def create_step(
        self,
        *,
        step_type: str,
        input_artifact_ids: list[str],
        output_artifact_id: str,
        actor_id: str,
        method: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._post(
            "/api/steps",
            {
                "step_type": step_type,
                "input_artifact_ids": input_artifact_ids,
                "output_artifact_id": output_artifact_id,
                "target_artifact_id": None,
                "actor_id": actor_id,
                "method": method,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        if not response:
            raise RuntimeError("dw_step_empty_response")
        return response


def _agent_actor_id(agent_id: str | None) -> str:
    clean = (agent_id or "unknown-agent").strip() or "unknown-agent"
    return f"researka:agent:{clean}"


def emit_publication_chain(
    *,
    submission: ResearchObject,
    publication: ResearchObject,
    review: ResearchObject | None,
    decision: ResearchObject | None,
    config: DerivationWebConfig | None = None,
) -> dict[str, Any]:
    resolved_config = config if config is not None else config_from_env()
    if resolved_config is None:
        return {}

    client = DerivationWebClient(resolved_config)
    agent_id = str(submission.metadata.get("author_agent_id") or "unknown-agent")
    agent_actor_id = _agent_actor_id(agent_id)

    client.ensure_actor(SYSTEM_ACTOR_ID, kind="system", name="Researka v2 gatekeeper")
    client.ensure_actor(agent_actor_id, kind="agent", name=agent_id)

    submission_artifact = client.create_artifact(
        kind="source",
        content_type="application/json",
        body_text=_canonical_json(
            {
                "id": submission.id,
                "title": submission.title,
                "body_markdown": submission.body_markdown,
                "metadata": submission.metadata,
                "created_at": submission.created_at,
            }
        ),
        metadata={
            "researka_object_type": submission.object_type.value,
            "researka_submission_id": submission.id,
            "title": submission.title,
            "author_agent_id": agent_id,
        },
        actor_id=agent_actor_id,
    )

    publication_artifact = client.create_artifact(
        kind="claim",
        content_type="text/markdown",
        body_text=publication.body_markdown,
        metadata={
            "researka_object_type": publication.object_type.value,
            "researka_publication_id": publication.id,
            "researka_submission_id": submission.id,
            "title": publication.title,
            "decision": "accept",
            "review_id": review.id if review is not None else None,
            "decision_id": decision.id if decision is not None else None,
            "author_agent_id": publication.metadata.get("author_agent_id"),
            "orcid": publication.metadata.get("orcid"),
            "article_type": publication.metadata.get("article_type"),
        },
        actor_id=SYSTEM_ACTOR_ID,
    )

    step = client.create_step(
        step_type="classify",
        input_artifact_ids=[str(submission_artifact["id"])],
        output_artifact_id=str(publication_artifact["id"]),
        actor_id=SYSTEM_ACTOR_ID,
        method={
            "decision": "accept",
            "researka_submission_id": submission.id,
            "researka_publication_id": publication.id,
            "review_id": review.id if review is not None else None,
            "decision_id": decision.id if decision is not None else None,
        },
    )

    artifact_id = str(publication_artifact["id"])
    content_hash = str(publication_artifact["content_hash"])
    return {
        "dw_artifact_id": artifact_id,
        "dw_chain_url": f"{resolved_config.base_url}/artifacts/{artifact_id}/chain",
        "dw_api_chain_url": f"{resolved_config.base_url}/api/artifacts/{artifact_id}/chain",
        "dw_source_artifact_id": submission_artifact["id"],
        "dw_step_id": step["id"],
        "dw_step_hash": step["step_hash"],
        "dw_status": "registered",
        "content_hash": f"sha256:{content_hash}",
        "sha256": f"sha256:{content_hash}",
    }


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
    config: DerivationWebConfig | None = None,
    emit_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Backfill DW metadata for existing publications.

    Dry-run is the default and never calls Derivation Web. Apply mode reuses the
    same emit path as new publications, then persists only returned DW metadata.
    """
    emit = emit_fn or emit_publication_chain
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
        if publication.metadata.get("dw_artifact_id"):
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
            item["status"] = "would_register"
            summary["items"].append(item)
            continue

        try:
            emitted = emit(
                submission=submission,
                publication=publication,
                review=review,
                decision=decision,
                config=config,
            )
            if not emitted:
                summary["skipped"] += 1
                item["status"] = "dw_not_configured"
                summary["items"].append(item)
                continue
            metadata = {**publication.metadata, **emitted}
            updated = repository.update_object_metadata(publication.id, metadata)
            if updated is None:
                raise RuntimeError("publication_disappeared")
            summary["registered"] += 1
            item.update(
                {
                    "status": "registered",
                    "dw_artifact_id": emitted.get("dw_artifact_id"),
                    "dw_chain_url": emitted.get("dw_chain_url"),
                    "sha256": emitted.get("sha256"),
                }
            )
        except Exception as exc:
            summary["failed"] += 1
            item["status"] = "failed"
            item["error"] = str(exc)[:240]
        summary["items"].append(item)

    return summary
