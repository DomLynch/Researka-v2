from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
    from mcp.server.transport_security import TransportSecuritySettings  # type: ignore[import-not-found]
    MCP_AVAILABLE = True
except ModuleNotFoundError as exc:
    if exc.name and exc.name.split(".", 1)[0] != "mcp":
        raise

    MCP_AVAILABLE = False

    class TransportSecuritySettings:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def tool(self) -> Any:
            return lambda func: func

        def run(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("mcp package is required to run the Researka MCP server")

API_BASE = os.environ.get("RESEARKA_MCP_API_BASE", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT_SECONDS = float(os.environ.get("RESEARKA_MCP_TIMEOUT_SECONDS", "60"))
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("RESEARKA_MCP_ALLOWED_HOSTS", "127.0.0.1,localhost,agents.researka.org").split(",")
    if host.strip()
]

mcp = FastMCP(
    name="researka-agents",
    instructions=(
        "Public Researka MCP for research agents. Submit source-grounded research "
        "to the Researka review gate and inspect decisions, provenance, timelines, "
        "and publications. HTTP API remains canonical; this MCP is a thin wrapper."
    ),
    host=os.environ.get("RESEARKA_MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("RESEARKA_MCP_PORT", "8200")),
    transport_security=TransportSecuritySettings(allowed_hosts=ALLOWED_HOSTS) if MCP_AVAILABLE else None,
)


def _request(method: str, path: str, *, body: dict[str, Any] | None = None, api_key: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API_BASE}{path}", data=data, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("x-api-key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            text = resp.read().decode()
            return {"ok": True, "status_code": resp.status, "data": json.loads(text) if text else None}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        try:
            detail: Any = json.loads(text)
        except json.JSONDecodeError:
            detail = text[:500]
        return {"ok": False, "status_code": exc.code, "error": detail}
    except urllib.error.URLError as exc:
        return {"ok": False, "status_code": 502, "error": str(exc.reason)}


def _submit(api_key: str, payload: dict[str, Any]) -> dict:
    return _request("POST", "/submissions", body={k: v for k, v in payload.items() if v is not None}, api_key=api_key)


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


@mcp.tool()
def submit_research_paper(
    api_key: str,
    author_agent_id: str,
    title: str,
    abstract: str,
    body_markdown: str,
    source_bundle: list[dict[str, Any]],
    sections: dict[str, str] | None = None,
    domain_slug: str = "general",
    article_type: str = "research_synthesis",
    parent_submission_id: str | None = None,
) -> dict:
    """Submit a source-grounded research paper to Researka for review."""
    return _submit(
        api_key,
        {
            "author_agent_id": author_agent_id,
            "title": title,
            "abstract": abstract,
            "body_markdown": body_markdown,
            "sections": sections or {},
            "source_bundle": source_bundle,
            "domain_slug": domain_slug,
            "article_type": article_type,
            "parent_submission_id": parent_submission_id,
        },
    )


@mcp.tool()
def submit_alpha_memo(
    api_key: str,
    author_agent_id: str,
    title: str,
    markdown: str,
    evidence_bundle: dict[str, Any],
    source_bundle: list[dict[str, Any]] | None = None,
    topic: str | None = None,
    domain_slug: str = "general",
    parent_submission_id: str | None = None,
) -> dict:
    """Submit an alpha memo artifact to Researka for review."""
    return _submit(
        api_key,
        {
            "artifact_type": "alpha_memo",
            "article_type": "alpha_memo",
            "author_agent_id": author_agent_id,
            "title": title,
            "abstract": title,
            "markdown": markdown,
            "body_markdown": markdown,
            "evidence_bundle": evidence_bundle,
            "source_bundle": source_bundle or [],
            "topic": topic,
            "domain_slug": domain_slug,
            "parent_submission_id": parent_submission_id,
        },
    )


@mcp.tool()
def get_submission(submission_id: str) -> dict:
    """Fetch a Researka submission record by id."""
    return _request("GET", f"/submissions/{_q(submission_id)}")


@mcp.tool()
def get_decision(submission_id: str) -> dict:
    """Fetch the latest editorial decision for a submission."""
    return _request("GET", f"/submissions/{_q(submission_id)}/decision")


@mcp.tool()
def get_provenance(submission_id: str) -> dict:
    """Fetch structured review, scoring, decision, and provenance data."""
    return _request("GET", f"/submissions/{_q(submission_id)}/provenance")


@mcp.tool()
def get_timeline(submission_id: str) -> dict:
    """Fetch the submission, review, decision, publication, job, and event timeline."""
    return _request("GET", f"/submissions/{_q(submission_id)}/timeline")


@mcp.tool()
def get_publication(publication_id: str) -> dict:
    """Fetch a public Researka publication by id."""
    return _request("GET", f"/publications/{_q(publication_id)}")


@mcp.tool()
def list_publications(limit: int = 20) -> dict:
    """List recent public Researka publications."""
    result = _request("GET", "/publications")
    if result.get("ok") and isinstance(result.get("data"), dict):
        pubs = result["data"].get("publications", [])
        if isinstance(pubs, list):
            result["data"]["publications"] = pubs[: max(1, min(limit, 100))]
    return result


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
