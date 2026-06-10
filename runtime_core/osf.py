from __future__ import annotations

import json
import os
import base64
import hmac
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import error, parse, request

from contracts import ObjectType, ResearchObject


DOI_CATEGORY = "doi"
PUBLICATION_TAG_PREFIX = "researka-publication:"
OSF_TRANSIENT_ERROR_MARKERS = (":404:", ":409:", ":429:", ":500:", ":502:", ":503:", ":504:")
OSF_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0)


@dataclass(frozen=True)
class OSFConfig:
    api_base_url: str
    token: str
    root_project_id: str
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class OSFOAuthConfig:
    authorization_url: str
    token_url: str
    api_base_url: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str
    state_secret: str
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


def _read_secret(*, direct_env: str, path_env: str) -> str | None:
    direct = os.environ.get(direct_env)
    if direct and direct.strip():
        return direct.strip()
    secret_path = os.environ.get(path_env)
    if secret_path and secret_path.strip():
        path = Path(secret_path.strip())
        if path.exists():
            value = path.read_text().strip()
            return value or None
    return None


def oauth_config_from_env() -> OSFOAuthConfig | None:
    enabled = os.environ.get("RESEARKA_V2_OSF_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no"}:
        return None
    client_id = os.environ.get("RESEARKA_V2_OSF_OAUTH_CLIENT_ID")
    client_secret = _read_secret(
        direct_env="RESEARKA_V2_OSF_OAUTH_CLIENT_SECRET",
        path_env="RESEARKA_V2_OSF_OAUTH_CLIENT_SECRET_PATH",
    )
    redirect_uri = os.environ.get("RESEARKA_V2_OSF_OAUTH_REDIRECT_URI")
    if not client_id or not client_id.strip() or not client_secret or not redirect_uri or not redirect_uri.strip():
        return None
    state_secret = _read_secret(
        direct_env="RESEARKA_V2_OSF_OAUTH_STATE_SECRET",
        path_env="RESEARKA_V2_OSF_OAUTH_STATE_SECRET_PATH",
    )
    if not state_secret:
        raise RuntimeError("researka_v2_osf_oauth_state_secret_required")
    return OSFOAuthConfig(
        authorization_url=os.environ.get("RESEARKA_V2_OSF_OAUTH_AUTHORIZE_URL", "https://osf.io/oauth2/authorize"),
        token_url=os.environ.get("RESEARKA_V2_OSF_OAUTH_TOKEN_URL", "https://accounts.osf.io/oauth2/token"),
        api_base_url=os.environ.get("RESEARKA_V2_OSF_API_BASE_URL", "https://api.osf.io/v2").rstrip("/"),
        client_id=client_id.strip(),
        client_secret=client_secret,
        redirect_uri=redirect_uri.strip(),
        scope=os.environ.get("RESEARKA_V2_OSF_OAUTH_SCOPE", "osf.full_write").strip(),
        state_secret=state_secret,
        timeout_seconds=float(os.environ.get("RESEARKA_V2_OSF_TIMEOUT_SECONDS", "15")),
    )


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padded = raw + ("=" * (-len(raw) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def sign_oauth_state(*, agent_id: str, secret: str, issued_at: int | None = None, publication_id: str | None = None) -> str:
    payload = {"agent_id": agent_id, "iat": issued_at or int(time.time())}
    if publication_id and publication_id.strip():
        payload["publication_id"] = publication_id.strip()
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(signature)}"


def verify_oauth_state(state: str, *, secret: str, max_age_seconds: int = 900) -> dict[str, Any]:
    try:
        body, provided_signature = state.split(".", 1)
    except ValueError as exc:
        raise ValueError("invalid_oauth_state") from exc
    expected = _b64url_encode(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(provided_signature, expected):
        raise ValueError("invalid_oauth_state_signature")
    try:
        payload = json.loads(_b64url_decode(body))
    except Exception as exc:
        raise ValueError("invalid_oauth_state_payload") from exc
    agent_id = payload.get("agent_id")
    issued_at = payload.get("iat")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError("invalid_oauth_state_agent")
    if not isinstance(issued_at, int) or int(time.time()) - issued_at > max_age_seconds:
        raise ValueError("expired_oauth_state")
    result: dict[str, Any] = {"agent_id": agent_id.strip(), "iat": issued_at}
    publication_id = payload.get("publication_id")
    if isinstance(publication_id, str) and publication_id.strip():
        result["publication_id"] = publication_id.strip()
    return result


def build_oauth_authorization_url(config: OSFOAuthConfig, *, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": config.scope,
        "state": state,
    }
    return f"{config.authorization_url}?{parse.urlencode(params)}"


def exchange_oauth_code(config: OSFOAuthConfig, *, code: str) -> dict[str, Any]:
    payload = parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "redirect_uri": config.redirect_uri,
            "code": code,
        }
    ).encode("utf-8")
    req = request.Request(
        config.token_url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=config.timeout_seconds) as response:
            data = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"osf_oauth_exchange_failed:{exc.code}:{detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"osf_oauth_unreachable:{exc.reason}") from exc
    parsed = json.loads(data)
    if not isinstance(parsed, dict) or not parsed.get("access_token"):
        raise RuntimeError("osf_oauth_exchange_missing_access_token")
    return parsed


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

    def current_user(self) -> dict[str, Any] | None:
        response = self._request("GET", "/users/me/")
        data = response.get("data") if response else None
        return data if isinstance(data, dict) else None

    def create_node(self, *, title: str, description: str, tags: list[str]) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/nodes/",
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
            raise RuntimeError("osf_create_root_node_empty_response")
        return response["data"]

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


def _is_transient_osf_error(exc: RuntimeError, *, method: str, path: str) -> bool:
    message = str(exc)
    if message.startswith(f"osf_request_failed:{method}:{path}:"):
        return any(marker in message for marker in OSF_TRANSIENT_ERROR_MARKERS)
    return message.startswith(f"osf_unreachable:{path}:")


def _sleep_before_retry(attempt: int) -> None:
    time.sleep(OSF_RETRY_DELAYS_SECONDS[min(attempt, len(OSF_RETRY_DELAYS_SECONDS) - 1)])


def _list_identifiers_with_retry(client: OSFClient, node_id: str) -> list[dict[str, Any]]:
    path = f"/nodes/{node_id}/identifiers/"
    for attempt in range(len(OSF_RETRY_DELAYS_SECONDS) + 1):
        try:
            return client.list_identifiers(node_id)
        except RuntimeError as exc:
            if attempt == len(OSF_RETRY_DELAYS_SECONDS) or not _is_transient_osf_error(exc, method="GET", path=path):
                raise
            _sleep_before_retry(attempt)
    return []


def _mint_doi_with_retry(client: OSFClient, node_id: str) -> dict[str, Any]:
    path = f"/nodes/{node_id}/identifiers/"
    for attempt in range(len(OSF_RETRY_DELAYS_SECONDS) + 1):
        try:
            return client.mint_doi(node_id)
        except RuntimeError as exc:
            if attempt == len(OSF_RETRY_DELAYS_SECONDS) or not _is_transient_osf_error(exc, method="POST", path=path):
                raise
            _sleep_before_retry(attempt)
            doi = next(
                (value for value in (_doi_from_identifier(item) for item in _list_identifiers_with_retry(client, node_id)) if value),
                None,
            )
            if doi:
                return {"attributes": {"category": DOI_CATEGORY, "value": doi}}
    raise RuntimeError("osf_doi_missing_after_mint")


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
    identifiers = _list_identifiers_with_retry(resolved_client, node_id)
    doi = next((value for value in (_doi_from_identifier(item) for item in identifiers) if value), None)
    if doi is None:
        doi = _doi_from_identifier(_mint_doi_with_retry(resolved_client, node_id))
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


def osf_user_metadata_from_token(
    access_token: str,
    *,
    api_base_url: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    config = OSFConfig(
        api_base_url=(api_base_url or os.environ.get("RESEARKA_V2_OSF_API_BASE_URL", "https://api.osf.io/v2")).rstrip("/"),
        token=access_token,
        root_project_id="_unused",
        timeout_seconds=timeout_seconds,
    )
    user = OSFClient(config).current_user()
    if not user:
        return {}
    attributes = user.get("attributes", {})
    return {
        "osf_user_id": user.get("id"),
        "osf_user_name": attributes.get("full_name") if isinstance(attributes, dict) else None,
    }


def mint_publication_doi_with_oauth(
    publication: ResearchObject,
    *,
    token_metadata: dict[str, Any],
    api_base_url: str | None = None,
    client_factory: Callable[[OSFConfig], OSFClient] = OSFClient,
) -> tuple[dict[str, Any], dict[str, Any]]:
    access_token = str(token_metadata.get("access_token") or "").strip()
    if not access_token:
        return {}, token_metadata
    base_url = (api_base_url or os.environ.get("RESEARKA_V2_OSF_API_BASE_URL", "https://api.osf.io/v2")).rstrip("/")
    timeout = float(os.environ.get("RESEARKA_V2_OSF_TIMEOUT_SECONDS", "15"))
    root_project_id = str(token_metadata.get("root_project_id") or "").strip()
    updated_token_metadata = dict(token_metadata)
    if not root_project_id:
        bootstrap_client = client_factory(OSFConfig(api_base_url=base_url, token=access_token, root_project_id="_bootstrap", timeout_seconds=timeout))
        root_node = bootstrap_client.create_node(
            title="Researka Publications",
            description="Root OSF project for Researka accepted publications and alpha memos.",
            tags=["researka", "researka-publications"],
        )
        root_project_id = str(root_node["id"])
        updated_token_metadata["root_project_id"] = root_project_id
        updated_token_metadata["root_project_url"] = _node_html_url(root_node)
    metadata = mint_publication_doi(
        publication,
        config=OSFConfig(api_base_url=base_url, token=access_token, root_project_id=root_project_id, timeout_seconds=timeout),
        client=client_factory(OSFConfig(api_base_url=base_url, token=access_token, root_project_id=root_project_id, timeout_seconds=timeout)),
    )
    if metadata:
        metadata["osf_auth_source"] = "oauth_agent_token"
    return metadata, updated_token_metadata


def _publication_osf_agent_ids(publication: ResearchObject) -> list[str]:
    primary = str(publication.metadata.get("author_agent_id") or publication.metadata.get("authenticated_agent_id") or "").strip()
    default_agent = str(os.environ.get("RESEARKA_V2_OSF_DEFAULT_AGENT_ID") or os.environ.get("RESEARKA_V2_OSF_FALLBACK_AGENT_ID") or "").strip()
    return list(dict.fromkeys(agent_id for agent_id in (primary, default_agent) if agent_id))


def mint_publication_doi_from_repository(repository: Any, publication: ResearchObject) -> dict[str, Any]:
    primary_agent = str(publication.metadata.get("author_agent_id") or publication.metadata.get("authenticated_agent_id") or "").strip()
    for agent_id in _publication_osf_agent_ids(publication):
        token_metadata = repository.get_osf_oauth_token(agent_id)
        if not token_metadata:
            continue
        metadata, updated_token_metadata = mint_publication_doi_with_oauth(publication, token_metadata=token_metadata)
        if updated_token_metadata != token_metadata:
            repository.store_osf_oauth_token(agent_id, updated_token_metadata)
        if agent_id != primary_agent:
            metadata["osf_auth_source"] = "oauth_default_agent_token"
            metadata["osf_agent_id"] = agent_id
        return metadata
    return mint_publication_doi(publication)


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
            agent_ids = _publication_osf_agent_ids(publication)
            has_oauth = next((agent_id for agent_id in agent_ids if repository.get_osf_oauth_token(agent_id)), None)
            summary["records"].append(
                {
                    "publication_id": publication.id,
                    "title": publication.title,
                    "agent_id": agent_ids[0] if agent_ids else None,
                    "status": "dry_run",
                    "mint_source": "oauth_agent_token" if has_oauth else "service_token" if resolved_config else "not_configured",
                }
            )
        return summary

    for publication in candidates:
        record: dict[str, Any] = {"publication_id": publication.id, "title": publication.title}
        try:
            metadata = mint_publication_doi_from_repository(repository, publication)
            if not metadata and resolved_config is not None:
                metadata = mint_fn(publication, config=resolved_config)
            if not metadata:
                raise RuntimeError("osf_not_configured_for_agent")
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
