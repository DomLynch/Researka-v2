from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import error, request

from contracts import ObjectType, ResearchObject


DOI_CATEGORY = "doi"
PUBLICATION_TAG_PREFIX = "researka-publication:"


@dataclass(frozen=True)
class OSFConfig:
    api_base_url: str
    token: str
    root_project_id: str
    timeout_seconds: float = 10.0


def _read_token() -> str | None:
    direct = os.environ.get("RESEARKA_V2_OSF_TOKEN") or os.environ.get("OSF_ACCESS_TOKEN")
    if direct and direct.strip():
        return direct.strip()
    token_path = os.environ.get("RESEARKA_V2_OSF_TOKEN_PATH")
    if token_path and token_path.strip():
        path = Path(token_path.strip())
        if path.exists():
            return path.read_text().strip()
    return None


def osf_publication_metadata_from_env() -> dict[str, Any]:
    enabled = os.environ.get("RESEARKA_V2_OSF_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
    root_project_id = os.environ.get("RESEARKA_V2_OSF_PROJECT_ID")
    token_configured = bool(_read_token())
    if not enabled:
        status = "disabled"
    elif root_project_id and token_configured:
        status = "pending_osf_export"
    else:
        status = "pending_osf_credentials"
    return {
        "doi": None,
        "doi_status": status,
        "osf_status": status,
        "osf_project_id": root_project_id,
        "osf_guid": None,
        "osf_url": None,
        "osf": {
            "enabled": enabled,
            "status": status,
            "project_id": root_project_id,
            "guid": None,
            "url": None,
        },
    }


def config_from_env() -> OSFConfig | None:
    enabled = os.environ.get("RESEARKA_V2_OSF_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no"}:
        return None
    token = _read_token()
    root_project_id = os.environ.get("RESEARKA_V2_OSF_PROJECT_ID")
    if not token or not root_project_id:
        return None
    base_url = os.environ.get("RESEARKA_V2_OSF_API_BASE_URL", "https://api.osf.io/v2").rstrip("/")
    timeout = float(os.environ.get("RESEARKA_V2_OSF_TIMEOUT_SECONDS", "15"))
    return OSFConfig(api_base_url=base_url, token=token, root_project_id=root_project_id.strip(), timeout_seconds=timeout)


class OSFClient:
    def __init__(self, config: OSFConfig) -> None:
        self.config = config

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        ok_statuses: set[int] | None = None,
    ) -> dict[str, Any] | None:
        ok_statuses = ok_statuses or {200, 201}
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            f"{self.config.api_base_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                status = response.status
                data = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"osf_request_failed:{method}:{path}:{exc.code}:{detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"osf_unreachable:{path}:{exc.reason}") from exc
        if status not in ok_statuses:
            raise RuntimeError(f"osf_request_failed:{method}:{path}:{status}:{data}")
        if not data:
            return None
        parsed = json.loads(data)
        return parsed if isinstance(parsed, dict) else None

    def list_child_nodes(self, node_id: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"/nodes/{node_id}/children/")
        data = response.get("data", []) if response else []
        return [item for item in data if isinstance(item, dict)]

    def create_child_node(self, node_id: str, *, title: str, description: str, tags: list[str]) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/nodes/{node_id}/children/",
            payload={
                "data": {
                    "type": "nodes",
                    "attributes": {
                        "title": title,
                        "category": "project",
                        "description": description,
                        "public": True,
                        "tags": tags,
                    },
                }
            },
        )
        if not response or not isinstance(response.get("data"), dict):
            raise RuntimeError("osf_create_node_empty_response")
        return response["data"]

    def update_node(self, node_id: str, *, public: bool) -> None:
        self._request(
            "PATCH",
            f"/nodes/{node_id}/",
            payload={"data": {"type": "nodes", "id": node_id, "attributes": {"public": public}}},
        )

    def list_identifiers(self, node_id: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"/nodes/{node_id}/identifiers/")
        data = response.get("data", []) if response else []
        return [item for item in data if isinstance(item, dict)]

    def mint_doi(self, node_id: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/nodes/{node_id}/identifiers/",
            payload={"data": {"type": "identifiers", "attributes": {"category": DOI_CATEGORY}}},
            ok_statuses={201},
        )
        if not response or not isinstance(response.get("data"), dict):
            raise RuntimeError("osf_mint_doi_empty_response")
        return response["data"]


def _publication_tag(publication_id: str) -> str:
    return f"{PUBLICATION_TAG_PREFIX}{publication_id}"


def _node_html_url(node: dict[str, Any]) -> str | None:
    links = node.get("links", {})
    if isinstance(links, dict) and isinstance(links.get("html"), str):
        return links["html"]
    node_id = node.get("id")
    return f"https://osf.io/{node_id}/" if node_id else None


def _doi_from_identifier(identifier: dict[str, Any]) -> str | None:
    attributes = identifier.get("attributes", {})
    if not isinstance(attributes, dict):
        return None
    if attributes.get("category") != DOI_CATEGORY:
        return None
    value = attributes.get("value")
    return str(value).strip() if value else None


def _find_publication_node(client: OSFClient, root_project_id: str, publication_id: str) -> dict[str, Any] | None:
    expected_tag = _publication_tag(publication_id)
    for node in client.list_child_nodes(root_project_id):
        attributes = node.get("attributes", {})
        tags = attributes.get("tags", []) if isinstance(attributes, dict) else []
        if expected_tag in tags:
            return node
    return None


def _publication_description(publication: ResearchObject) -> str:
    abstract = str(publication.metadata.get("abstract") or "").strip()
    return "\n\n".join(
        part
        for part in (
            f"Researka accepted publication: {publication.id}",
            f"Source submission: {publication.parent_object_id}" if publication.parent_object_id else "",
            abstract,
        )
        if part
    )


def mint_publication_doi(
    publication: ResearchObject,
    *,
    config: OSFConfig | None = None,
    client: OSFClient | None = None,
) -> dict[str, Any]:
    resolved_config = config if config is not None else config_from_env()
    if resolved_config is None:
        return {}
    resolved_client = client if client is not None else OSFClient(resolved_config)
    node = _find_publication_node(resolved_client, resolved_config.root_project_id, publication.id)
    if node is None:
        node = resolved_client.create_child_node(
            resolved_config.root_project_id,
            title=publication.title,
            description=_publication_description(publication),
            tags=["researka", "researka-publication", _publication_tag(publication.id)],
        )
    node_id = str(node["id"])
    resolved_client.update_node(node_id, public=True)
    identifiers = resolved_client.list_identifiers(node_id)
    doi = next((value for value in (_doi_from_identifier(item) for item in identifiers) if value), None)
    if doi is None:
        doi = _doi_from_identifier(resolved_client.mint_doi(node_id))
    if not doi:
        raise RuntimeError("osf_doi_missing_after_mint")
    osf_url = _node_html_url(node)
    return {
        "doi": doi,
        "doi_status": "minted",
        "osf_status": "minted",
        "osf_project_id": resolved_config.root_project_id,
        "osf_guid": node_id,
        "osf_url": osf_url,
        "osf": {
            "enabled": True,
            "status": "minted",
            "project_id": resolved_config.root_project_id,
            "guid": node_id,
            "url": osf_url,
            "doi": doi,
        },
    }


def backfill_missing_publication_dois(
    repository: Any,
    *,
    apply: bool = False,
    limit: int | None = None,
    publication_id: str | None = None,
    config: OSFConfig | None = None,
    mint_fn: Callable[..., dict[str, Any]] = mint_publication_doi,
) -> dict[str, Any]:
    resolved_config = config if config is not None else (config_from_env() if apply else None)
    publications = repository.list_objects(ObjectType.PUBLICATION)
    if publication_id is not None:
        publications = [publication for publication in publications if publication.id == publication_id]

    candidates: list[ResearchObject] = []
    for publication in publications:
        if publication.metadata.get("doi") or publication.metadata.get("osf_status") == "minted":
            continue
        candidates.append(publication)
        if limit is not None and len(candidates) >= limit:
            break

    summary: dict[str, Any] = {
        "apply": apply,
        "eligible": len(candidates),
        "minted": 0,
        "failed": 0,
        "records": [],
    }
    if not apply:
        for publication in candidates:
            summary["records"].append({"publication_id": publication.id, "title": publication.title, "status": "dry_run"})
        return summary
    if resolved_config is None:
        raise RuntimeError("osf_not_configured")

    for publication in candidates:
        record: dict[str, Any] = {"publication_id": publication.id, "title": publication.title}
        try:
            metadata = mint_fn(publication, config=resolved_config)
            updated = repository.update_object_metadata(publication.id, {**publication.metadata, **metadata})
            if updated is None:
                raise RuntimeError("publication_disappeared")
            record["status"] = "minted"
            record["doi"] = metadata.get("doi")
            summary["minted"] += 1
        except Exception as exc:
            repository.update_object_metadata(
                publication.id,
                {**publication.metadata, "osf_status": "failed", "doi_status": "failed", "osf_error": str(exc)[:240]},
            )
            record["status"] = "failed"
            record["error"] = str(exc)[:240]
            summary["failed"] += 1
        summary["records"].append(record)
    return summary
