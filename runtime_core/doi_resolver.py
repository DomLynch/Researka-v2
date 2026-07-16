from __future__ import annotations

import logging
import os
import socket
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from ipaddress import ip_address
from typing import Any

import httpx

log = logging.getLogger(__name__)


class UnsafeSourceLocator(ValueError):
    pass

# Existence is checked against the doi.org handle system, which is
# registrar-agnostic (Crossref, DataCite, mEDRA, ...). A Crossref-only
# resolver would false-flag real DOIs from other registrars, and the local
# corpus is topic-sliced so it cannot arbitrate global existence.


def _base_url() -> str:
    return os.getenv("RESEARKA_DOI_RESOLVER_URL", "https://doi.org/api/handles").rstrip("/")


def _enabled() -> bool:
    return os.getenv("RESEARKA_DOI_CHECK_ENABLED", "1") == "1"


def _timeout_s() -> float:
    try:
        return float(os.getenv("RESEARKA_DOI_CHECK_TIMEOUT_S", "3"))
    except ValueError:
        return 3.0


def _max_dois() -> int:
    try:
        return max(1, int(os.getenv("RESEARKA_DOI_CHECK_MAX", "25")))
    except ValueError:
        return 25


def _max_sources() -> int:
    try:
        return max(1, int(os.getenv("RESEARKA_SOURCE_CHECK_MAX", "100")))
    except ValueError:
        return 100


def _source_enabled() -> bool:
    return os.getenv("RESEARKA_SOURCE_CHECK_ENABLED", os.getenv("RESEARKA_DOI_CHECK_ENABLED", "1")) == "1"


def _unavailable_recommendation() -> str:
    # Mirrors the integrity client: outages never silently pass. The result is
    # always stamped available=False; RESEARKA_DOI_CHECK_FAIL_CLOSED=1
    # additionally holds the submission (revise) instead of proceeding.
    return "revise" if os.getenv("RESEARKA_DOI_CHECK_FAIL_CLOSED", "0") == "1" else "pass"


def resolve_dois(dois: list[str]) -> dict[str, Any] | None:
    """Check that every DOI is registered in the global handle system.

    Returns None when disabled or nothing to check; otherwise a stamped result:
    {available, recommendation, checked, missing[, reason]}.
    """
    if not _enabled() or not dois:
        return None
    checked: list[str] = []
    missing: list[str] = []
    try:
        with httpx.Client(timeout=_timeout_s(), follow_redirects=True) as client:
            for doi in dois[: _max_dois()]:
                # Encode the DOI as a path segment ('/' stays); a stray '?' or
                # '#' must not truncate the handle lookup into a false 404.
                response = client.get(f"{_base_url()}/{urllib.parse.quote(doi, safe='/')}")
                if response.status_code == 404:
                    missing.append(doi)
                else:
                    response.raise_for_status()
                checked.append(doi)
    except Exception as exc:
        log.warning("doi_resolution_unavailable", extra={"error": str(exc)})
        return {
            "available": False,
            "recommendation": _unavailable_recommendation(),
            "reason": f"doi_resolver_unavailable: {exc}",
            "checked": checked,
            "missing": missing,
        }
    return {
        "available": True,
        "recommendation": "reject" if missing else "pass",
        "checked": checked,
        "missing": missing,
    }


def _non_doi_locator(source: dict[str, Any]) -> str | None:
    if source.get("doi"):
        return None
    if pmid := str(source.get("pmid") or "").strip():
        return f"https://pubmed.ncbi.nlm.nih.gov/{urllib.parse.quote(pmid, safe='')}/"
    if openalex := str(source.get("openalex_id") or "").strip():
        return openalex if openalex.startswith("http") else f"https://openalex.org/{urllib.parse.quote(openalex, safe='')}"
    if registry := str(source.get("registry_id") or "").strip():
        return str(source.get("url") or "").strip() or (
            "https://trialsearch.who.int/Trial2.aspx?TrialID=" + urllib.parse.quote(registry, safe="")
        )
    url = str(source.get("url") or "").strip()
    return url or None


def _require_public_source(request: httpx.Request) -> None:
    host = request.url.host
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    if request.url.scheme not in {"http", "https"} or not host or port not in {80, 443}:
        raise UnsafeSourceLocator("source locator must use public HTTP(S)")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ip_address(address[4][0]).is_global for address in addresses):
        raise UnsafeSourceLocator("source locator resolves to a non-public address")


def resolve_source_locators(sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve every non-DOI source locator without serially blocking intake."""
    if not _source_enabled():
        return None
    locators = list(dict.fromkeys(filter(None, (_non_doi_locator(source) for source in sources))))[: _max_sources()]
    if not locators:
        return None

    def check(client: httpx.Client, locator: str) -> tuple[str, str]:
        try:
            status = client.get(locator, headers={"Range": "bytes=0-0"}).status_code
        except UnsafeSourceLocator:
            return locator, "missing"
        except Exception:
            return locator, "unavailable"
        if status in {404, 410}:
            return locator, "missing"
        if status == 429 or status >= 500:
            return locator, "unavailable"
        return locator, "ok"

    try:
        with httpx.Client(
            timeout=_timeout_s(),
            follow_redirects=True,
            event_hooks={"request": [_require_public_source]},
        ) as client:
            with ThreadPoolExecutor(max_workers=min(8, len(locators))) as pool:
                results = list(pool.map(lambda locator: check(client, locator), locators))
    except Exception as exc:
        log.warning("source_resolution_unavailable", extra={"error": str(exc)})
        return {
            "available": False,
            "recommendation": _unavailable_recommendation(),
            "reason": f"source_resolver_unavailable: {exc}",
            "checked": [],
            "missing": [],
        }
    missing = [locator for locator, status in results if status == "missing"]
    unavailable = [locator for locator, status in results if status == "unavailable"]
    return {
        "available": not unavailable,
        "recommendation": _unavailable_recommendation() if unavailable else "reject" if missing else "pass",
        "checked": [locator for locator, status in results if status != "unavailable"],
        "missing": missing,
        **({"reason": f"source_resolver_unavailable:{len(unavailable)}"} if unavailable else {}),
    }
