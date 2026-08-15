from __future__ import annotations

import json
import logging
import os
import base64
import hmac
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import error, parse, request

from contracts import ArticleType, Decision, ObjectType, ResearchObject
from .compiler import canonical_package_hash
from .doi_resolver import source_identity
from .judge_release import judge_release_manifest_valid
from .review_contract import accept_quorum_satisfied
from .urls import validated_service_url


log = logging.getLogger(__name__)
DOI_CATEGORY = "doi"
PUBLICATION_TAG_PREFIX = "researka-publication:"
OSF_TRANSIENT_ERROR_MARKERS = (":404:", ":409:", ":429:", ":500:", ":502:", ":503:", ":504:")
OSF_TRANSIENT_TEXT_MARKERS = ("timed out", "timeout", "temporarily unavailable", "connection reset")
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
        authorization_url=validated_service_url(
            os.environ.get("RESEARKA_V2_OSF_OAUTH_AUTHORIZE_URL", "https://osf.io/oauth2/authorize"),
            label="osf_authorization",
        ),
        token_url=validated_service_url(
            os.environ.get("RESEARKA_V2_OSF_OAUTH_TOKEN_URL", "https://accounts.osf.io/oauth2/token"),
            label="osf_token",
        ),
        api_base_url=validated_service_url(
            os.environ.get("RESEARKA_V2_OSF_API_BASE_URL", "https://api.osf.io/v2"),
            label="osf_api",
        ),
        client_id=client_id.strip(),
        client_secret=client_secret,
        redirect_uri=validated_service_url(redirect_uri, label="osf_redirect"),
        scope=os.environ.get("RESEARKA_V2_OSF_OAUTH_SCOPE", "osf.full_write").strip(),
        state_secret=state_secret,
        timeout_seconds=float(os.environ.get("RESEARKA_V2_OSF_TIMEOUT_SECONDS", "15")),
    )


def warn_if_osf_default_owner_missing() -> None:
    if os.environ.get("RESEARKA_V2_OSF_DEFAULT_AGENT_ID") or os.environ.get("RESEARKA_V2_OSF_FALLBACK_AGENT_ID"):
        return
    if os.environ.get("RESEARKA_V2_OSF_OAUTH_CLIENT_ID") or os.environ.get("RESEARKA_V2_OSF_TOKEN_ENCRYPTION_KEY_PATH"):
        log.warning("osf_default_owner_agent_missing")


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
    base_url = validated_service_url(config.authorization_url, label="osf_authorization")
    return f"{base_url}?{parse.urlencode(params)}"


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
        validated_service_url(config.token_url, label="osf_token"),
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=config.timeout_seconds) as response:  # nosec B310 - token URL validated above
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
    base_url = validated_service_url(
        os.environ.get("RESEARKA_V2_OSF_API_BASE_URL", "https://api.osf.io/v2"),
        label="osf_api",
    )
    timeout = float(os.environ.get("RESEARKA_V2_OSF_TIMEOUT_SECONDS", "15"))
    return OSFConfig(api_base_url=base_url, token=token, root_project_id=root_project_id.strip(), timeout_seconds=timeout)


class OSFClient:
    def __init__(self, config: OSFConfig) -> None:
        self.config = OSFConfig(
            api_base_url=validated_service_url(config.api_base_url, label="osf_api"),
            token=config.token,
            root_project_id=config.root_project_id,
            timeout_seconds=config.timeout_seconds,
        )

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
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:  # nosec B310 - config URL validated at construction
                status = response.status
                data = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"osf_request_failed:{method}:{path}:{exc.code}:{detail}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"osf_timeout:{method}:{path}:{exc}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"osf_unreachable:{path}:{exc.reason}") from exc
        if status not in ok_statuses:
            raise RuntimeError(f"osf_request_failed:{method}:{path}:{status}:{data}")
        if not data:
            return None
        parsed = json.loads(data)
        return parsed if isinstance(parsed, dict) else None

    def _url_bytes(self, method: str, url: str, *, body: bytes | None = None) -> bytes:
        parsed_url = parse.urlparse(url)
        host = (parsed_url.hostname or "").lower()
        if parsed_url.scheme != "https" or not (host == "osf.io" or host.endswith(".osf.io")):
            raise RuntimeError("osf_untrusted_file_url")
        req = request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {self.config.token}"},
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:  # nosec B310 - OSF HTTPS host allowlist above
                return response.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"osf_file_request_failed:{method}:{exc.code}:{detail}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"osf_file_timeout:{method}:{exc}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"osf_file_unreachable:{exc.reason}") from exc

    def _url_json(self, url: str) -> dict[str, Any]:
        raw = self._url_bytes("GET", url)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError("osf_file_response_not_object")
        return parsed

    def _url_json_with_retry(self, url: str) -> dict[str, Any]:
        for attempt in range(len(OSF_RETRY_DELAYS_SECONDS) + 1):
            try:
                return self._url_json(url)
            except (RuntimeError, OSError) as exc:
                if attempt == len(OSF_RETRY_DELAYS_SECONDS) or not _is_transient_osf_error(exc, method="GET", path=url):
                    raise
                _sleep_before_retry(attempt)
        raise RuntimeError("osf_page_retry_exhausted")

    def list_child_nodes(self, node_id: str, *, tag: str | None = None) -> list[dict[str, Any]]:
        query: dict[str, str | int] = {"page[size]": 100}
        if tag:
            query["filter[tags]"] = tag
        response = self._request("GET", f"/nodes/{node_id}/children/?{parse.urlencode(query)}")
        nodes: list[dict[str, Any]] = []
        seen: set[str] = set()
        while response:
            nodes.extend(item for item in response.get("data", []) if isinstance(item, dict))
            links = response.get("links", {})
            next_url = links.get("next") if isinstance(links, dict) else None
            if not isinstance(next_url, str) or not next_url or next_url in seen:
                break
            seen.add(next_url)
            response = self._url_json_with_retry(next_url)
        return nodes

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
                        "public": False,
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

    def upload_package(
        self,
        node_id: str,
        files: dict[str, str],
        *,
        release_id: str,
    ) -> list[dict[str, Any]]:
        upload_root, files_url = self._storage_links(node_id)
        existing = self._url_json(files_url).get("data", [])
        by_name = {
            str(item.get("attributes", {}).get("name")): item
            for item in existing
            if isinstance(item, dict) and isinstance(item.get("attributes"), dict)
        }
        receipts: list[dict[str, Any]] = []
        prefix = f"release-{release_id[:16]}-"
        for logical_name, text in sorted(files.items()):
            name = f"{prefix}{logical_name}"
            raw = text.encode("utf-8")
            current = by_name.get(name)
            current_links = current.get("links", {}) if isinstance(current, dict) else {}
            if current is not None:
                download_url = current_links.get("download") if isinstance(current_links, dict) else None
                if not isinstance(download_url, str):
                    raise RuntimeError(f"osf_package_existing_file_unverifiable:{logical_name}")
                if self._url_bytes("GET", download_url) != raw:
                    raise RuntimeError(f"osf_package_release_conflict:{logical_name}")
                receipts.append({
                    "name": name,
                    "logical_name": logical_name,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                })
                continue
            upload_url = current_links.get("upload") if isinstance(current_links, dict) else None
            if isinstance(upload_url, str):
                separator = "&" if "?" in upload_url else "?"
                upload_url = f"{upload_url}{separator}{parse.urlencode({'kind': 'file'})}"
            else:
                separator = "&" if "?" in upload_root else "?"
                upload_url = f"{upload_root}{separator}{parse.urlencode({'kind': 'file', 'name': name})}"
            response = json.loads(self._url_bytes("PUT", upload_url, body=raw).decode("utf-8"))
            file_data = response.get("data", response) if isinstance(response, dict) else {}
            file_links = file_data.get("links", {}) if isinstance(file_data, dict) else {}
            download_url = file_links.get("download") if isinstance(file_links, dict) else None
            if not isinstance(download_url, str) or self._url_bytes("GET", download_url) != raw:
                raise RuntimeError(f"osf_package_verification_failed:{logical_name}")
            receipts.append({
                "name": name,
                "logical_name": logical_name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            })
        return receipts

    def _storage_links(self, node_id: str) -> tuple[str, str]:
        for attempt in range(len(OSF_RETRY_DELAYS_SECONDS) + 1):
            providers = self._request("GET", f"/nodes/{node_id}/files/") or {}
            provider = next(
                (
                    item
                    for item in providers.get("data", [])
                    if isinstance(item, dict)
                    and (
                        item.get("id") == "osfstorage"
                        or (
                            isinstance(item.get("attributes"), dict)
                            and item["attributes"].get("provider") == "osfstorage"
                        )
                    )
                ),
                None,
            )
            links = provider.get("links", {}) if isinstance(provider, dict) else {}
            relationships = provider.get("relationships", {}) if isinstance(provider, dict) else {}
            files = relationships.get("files", {}) if isinstance(relationships, dict) else {}
            file_links = files.get("links", {}) if isinstance(files, dict) else {}
            related = file_links.get("related", {}) if isinstance(file_links, dict) else {}
            upload_root = links.get("upload") if isinstance(links, dict) else None
            files_url = links.get("files") if isinstance(links, dict) else None
            files_url = files_url or (related.get("href") if isinstance(related, dict) else None)
            if isinstance(upload_root, str) and isinstance(files_url, str):
                return upload_root, files_url
            if attempt < len(OSF_RETRY_DELAYS_SECONDS):
                _sleep_before_retry(attempt)
        raise RuntimeError("osf_storage_links_missing")


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
    for node in client.list_child_nodes(root_project_id, tag=expected_tag):
        attributes = node.get("attributes", {})
        tags = attributes.get("tags", []) if isinstance(attributes, dict) else []
        if expected_tag in tags:
            return node
    return None


def _is_transient_osf_error(exc: Exception, *, method: str, path: str) -> bool:
    message = str(exc)
    if any(marker in message.lower() for marker in OSF_TRANSIENT_TEXT_MARKERS):
        return True
    if message.startswith(f"osf_timeout:{method}:{path}:"):
        return True
    if message.startswith(f"osf_request_failed:{method}:{path}:"):
        return any(marker in message for marker in OSF_TRANSIENT_ERROR_MARKERS)
    if message.startswith(f"osf_file_request_failed:{method}:"):
        return any(marker in message for marker in OSF_TRANSIENT_ERROR_MARKERS)
    if message.startswith(f"osf_file_timeout:{method}:"):
        return True
    if message.startswith("osf_file_unreachable:"):
        return True
    return message.startswith(f"osf_unreachable:{path}:")


def _sleep_before_retry(attempt: int) -> None:
    time.sleep(OSF_RETRY_DELAYS_SECONDS[min(attempt, len(OSF_RETRY_DELAYS_SECONDS) - 1)])


def _list_identifiers_with_retry(client: OSFClient, node_id: str) -> list[dict[str, Any]]:
    path = f"/nodes/{node_id}/identifiers/"
    for attempt in range(len(OSF_RETRY_DELAYS_SECONDS) + 1):
        try:
            return client.list_identifiers(node_id)
        except (RuntimeError, OSError) as exc:
            if attempt == len(OSF_RETRY_DELAYS_SECONDS) or not _is_transient_osf_error(exc, method="GET", path=path):
                raise
            _sleep_before_retry(attempt)
    return []


def _mint_doi_with_retry(client: OSFClient, node_id: str) -> dict[str, Any]:
    path = f"/nodes/{node_id}/identifiers/"
    for attempt in range(len(OSF_RETRY_DELAYS_SECONDS) + 1):
        try:
            return client.mint_doi(node_id)
        except (RuntimeError, OSError) as exc:
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


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"


def build_publication_package(repository: Any, publication: ResearchObject) -> dict[str, str]:
    submission = repository.get_object(str(publication.parent_object_id or ""))
    decision = repository.get_object(str(publication.metadata.get("decision_id") or ""))
    review = repository.get_object(str(publication.metadata.get("review_id") or ""))
    package_hash = str(publication.metadata.get("canonical_package_hash") or "")
    source_bundle = submission.metadata.get("source_bundle") if submission else None
    sections = submission.metadata.get("sections") if submission else None
    expected_hash = (
        canonical_package_hash(
            title=submission.title,
            abstract=str(submission.metadata.get("abstract") or ""),
            sections=sections,
            source_bundle=source_bundle,
            article_type=str(
                submission.metadata.get(
                    "article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value
                )
            ),
        )
        if submission is not None
        and isinstance(sections, dict)
        and isinstance(source_bundle, list)
        else ""
    )
    if (
        submission is None
        or submission.object_type != ObjectType.SUBMISSION
        or decision is None
        or decision.object_type != ObjectType.DECISION
        or decision.parent_object_id != submission.id
        or decision.metadata.get("decision") != Decision.ACCEPT.value
        or decision.metadata.get("superseded_by")
        or review is None
        or review.object_type != ObjectType.REVIEW
        or review.parent_object_id != submission.id
        or decision.metadata.get("review_id") != review.id
        or not package_hash.startswith("sha256:")
        or expected_hash != package_hash
        or decision.metadata.get("canonical_package_hash") != package_hash
        or review.metadata.get("reviewed_package_hash") != package_hash
        or publication.metadata.get("judge_release_id")
        != review.metadata.get("judge_release_id")
        or not judge_release_manifest_valid(review.metadata.get("judge_release"))
        or not accept_quorum_satisfied(review.metadata)
    ):
        raise RuntimeError("osf_scientific_package_lineage_invalid")
    sources = source_bundle if isinstance(source_bundle, list) else []
    verification = submission.metadata.get("source_verification")
    verification = verification if isinstance(verification, dict) else {}
    verified_evidence = {str(item) for item in verification.get("evidence_text_verified", [])}
    files = {
        "manuscript.md": publication.body_markdown.rstrip() + "\n",
        "source_bundle.json": _json_text(sources),
        "verified_evidence_spans.json": _json_text([
            {
                "source_id": source.get("source_id") or source.get("doi") or source.get("pmid") or source.get("openalex_id"),
                "evidence_text": source.get("evidence_span") or source.get("quote") or source.get("excerpt"),
                "evidence_text_verified": source_identity(source) in verified_evidence,
                "source_content_hash": source.get("source_content_hash"),
            }
            for source in sources
            if isinstance(source, dict)
        ]),
        "review_receipts.json": _json_text({"body_markdown": review.body_markdown, "metadata": review.metadata}),
        "decision.json": _json_text({"id": decision.id, "metadata": decision.metadata}),
        "policy_release.json": _json_text(review.metadata.get("judge_release") or {
            "judge_release_id": review.metadata.get("judge_release_id"),
            "policy_version": review.metadata.get("policy_version"),
        }),
        "ro-crate-metadata.json": _json_text({
            "@context": "https://w3id.org/ro/crate/1.1/context",
            "@graph": [{
                "@id": "./",
                "@type": "Dataset",
                "name": publication.title,
                "identifier": publication.id,
                "canonicalPackageHash": package_hash,
            }],
        }),
    }
    manifest = {
        "publication_id": publication.id,
        "submission_id": submission.id,
        "review_id": review.id,
        "decision_id": decision.id,
        "canonical_package_hash": package_hash,
        "files": {
            name: {"sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "size": len(content.encode("utf-8"))}
            for name, content in sorted(files.items())
        },
    }
    files["manifest-sha256.json"] = _json_text(manifest)
    return files


def mint_publication_doi(
    publication: ResearchObject,
    *,
    package_files: dict[str, str],
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
    package_hash = str(publication.metadata.get("canonical_package_hash") or "").removeprefix("sha256:")
    if not package_hash or "manifest-sha256.json" not in package_files:
        raise RuntimeError("osf_scientific_package_missing")
    package_receipts = resolved_client.upload_package(node_id, package_files, release_id=package_hash)
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
        "osf_package_hash": f"sha256:{hashlib.sha256(package_files['manifest-sha256.json'].encode()).hexdigest()}",
        "osf_package_files": package_receipts,
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
    package_files: dict[str, str],
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
        package_files=package_files,
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
    package_files = build_publication_package(repository, publication)
    primary_agent = str(publication.metadata.get("author_agent_id") or publication.metadata.get("authenticated_agent_id") or "").strip()
    for agent_id in _publication_osf_agent_ids(publication):
        token_metadata = repository.get_osf_oauth_token(agent_id)
        if not token_metadata:
            continue
        metadata, updated_token_metadata = mint_publication_doi_with_oauth(
            publication,
            token_metadata=token_metadata,
            package_files=package_files,
        )
        if updated_token_metadata != token_metadata:
            repository.store_osf_oauth_token(agent_id, updated_token_metadata)
        if agent_id != primary_agent:
            metadata["osf_auth_source"] = "oauth_default_agent_token"
            metadata["osf_agent_id"] = agent_id
        return metadata
    return mint_publication_doi(publication, package_files=package_files)


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
                metadata = mint_fn(
                    publication,
                    package_files=build_publication_package(repository, publication),
                    config=resolved_config,
                )
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
