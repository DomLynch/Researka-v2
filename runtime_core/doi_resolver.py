from __future__ import annotations

import html
import logging
import os
import re
import socket
import time
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
        return max(1, int(os.getenv("RESEARKA_DOI_CHECK_MAX", "100")))
    except ValueError:
        return 100


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
    return "revise" if os.getenv("RESEARKA_DOI_CHECK_FAIL_CLOSED", "1") == "1" else "pass"


def _metadata_enabled() -> bool:
    return os.getenv("RESEARKA_SOURCE_METADATA_CHECK_ENABLED", "1") == "1"


def _metadata_unavailable_recommendation() -> str:
    value = os.getenv(
        "RESEARKA_SOURCE_METADATA_FAIL_CLOSED",
        os.getenv("RESEARKA_DOI_CHECK_FAIL_CLOSED", "1"),
    )
    return "revise" if value == "1" else "pass"


def _metadata_attempts() -> int:
    try:
        return max(1, int(os.getenv("RESEARKA_SOURCE_METADATA_MAX_ATTEMPTS", "3")))
    except ValueError:
        return 3


def _registry_payload(client: httpx.Client, url: str) -> dict[str, Any] | None:
    attempts = _metadata_attempts()
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url)
            if response.status_code == 404:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
                    continue
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except httpx.HTTPStatusError:
            return None
        except (httpx.TransportError, OSError, ValueError):
            if attempt == attempts:
                return None
            time.sleep(0.5 * attempt)
    return None


_GENERIC_WORDS = {
    "about", "analysis", "article", "evidence", "effects", "results", "review", "study", "trial",
}


def _tokens(value: object) -> set[str]:
    clean = html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).lower()
    return {
        word for word in re.findall(r"[a-z0-9]+", clean)
        if len(word) >= 4 and word not in _GENERIC_WORDS
    }


def _text_matches(left: object, right: object, *, floor: float) -> bool:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    minimum_overlap = 1 if min(len(left_tokens), len(right_tokens)) == 1 else 2
    return overlap >= minimum_overlap and overlap / min(len(left_tokens), len(right_tokens)) >= floor


def _openalex_abstract(payload: dict[str, Any]) -> str:
    index = payload.get("abstract_inverted_index")
    if not isinstance(index, dict):
        return ""
    positioned = [
        (position, str(word))
        for word, positions in index.items()
        if isinstance(positions, list)
        for position in positions
        if isinstance(position, int)
    ]
    return " ".join(word for _, word in sorted(positioned))


def _crossref_retracted(payload: dict[str, Any]) -> bool:
    relation = payload.get("relation")
    if isinstance(relation, dict) and any("retract" in str(key).lower() for key in relation):
        return True
    updates = payload.get("update-to")
    if isinstance(updates, list) and any("retract" in str(item.get("type", "")).lower() for item in updates if isinstance(item, dict)):
        return True
    titles = payload.get("title")
    title = titles[0] if isinstance(titles, list) and titles else titles
    return any(
        word.startswith("retract")
        for word in html.unescape(re.sub(r"<[^>]+>", " ", str(title or ""))).lower().split()[:2]
    )


def _source_identity(source: dict[str, Any]) -> tuple[str, str | None, str | None] | None:
    if doi := str(source.get("doi") or "").strip().lower():
        return f"doi:{doi}", doi, f"https://doi.org/{urllib.parse.quote(doi, safe='')}"
    if pmid := str(source.get("pmid") or "").strip():
        return f"pmid:{pmid}", None, f"pmid:{urllib.parse.quote(pmid, safe='')}"
    if openalex := str(source.get("openalex_id") or "").strip():
        work_id = openalex.rstrip("/").rsplit("/", 1)[-1]
        return f"openalex:{work_id.lower()}", None, urllib.parse.quote(work_id, safe="")
    url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    path = urllib.parse.unquote(parsed.path).strip("/")
    if host.endswith("doi.org") and re.match(r"^10\.\d{4,}/\S+$", path):
        return f"doi:{path.lower()}", path.lower(), f"https://doi.org/{urllib.parse.quote(path, safe='')}"
    if host.endswith("pubmed.ncbi.nlm.nih.gov") and path.isdigit():
        return f"pmid:{path}", None, f"pmid:{path}"
    if host.endswith("openalex.org") and re.fullmatch(r"W\d+", path, re.I):
        return f"openalex:{path.lower()}", None, urllib.parse.quote(path, safe="")
    if registry := str(source.get("registry_id") or "").strip():
        return f"registry:{registry.lower()}", None, None
    if url:
        return f"url:{url.lower().rstrip('/')}", None, None
    return None


def verify_source_metadata(sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Verify registered source identity, evidence text, and retraction state."""
    if not _metadata_enabled():
        return None
    candidates = [(source, identity) for source in sources if (identity := _source_identity(source))][:_max_sources()]
    if not candidates:
        return None

    crossref_base = os.getenv("RESEARKA_CROSSREF_URL", "https://api.crossref.org/works").rstrip("/")
    openalex_base = os.getenv("RESEARKA_OPENALEX_URL", "https://api.openalex.org/works").rstrip("/")

    def check(client: httpx.Client, item: tuple[dict[str, Any], tuple[str, str | None, str | None]]) -> dict[str, Any]:
        source, (identity, doi, openalex_id) = item
        titles: list[str] = []
        abstracts: list[str] = []
        retracted = False
        authority_count = 0
        if doi:
            url = f"{crossref_base}/{urllib.parse.quote(doi, safe='')}"
            if mailto := os.getenv("RESEARKA_CROSSREF_MAILTO"):
                url += "?" + urllib.parse.urlencode({"mailto": mailto})
            payload = _registry_payload(client, url)
            message = payload.get("message", {}) if payload else {}
            if isinstance(message, dict) and message:
                authority_count += 1
                raw_titles = message.get("title")
                if isinstance(raw_titles, list):
                    titles.extend(str(value) for value in raw_titles if value)
                if message.get("abstract"):
                    abstracts.append(str(message["abstract"]))
                retracted = retracted or _crossref_retracted(message)
        if openalex_id and not authority_count:
            payload = _registry_payload(client, f"{openalex_base}/{openalex_id}")
            if payload:
                authority_count += 1
                if payload.get("title") or payload.get("display_name"):
                    titles.append(str(payload.get("title") or payload.get("display_name")))
                if abstract := _openalex_abstract(payload):
                    abstracts.append(abstract)
                retracted = retracted or bool(payload.get("is_retracted"))
        evidence = [
            str(source.get(key) or "").strip()
            for key in ("quote", "evidence_span", "excerpt")
            if str(source.get(key) or "").strip()
        ]
        return {
            "identity": identity,
            "checked": authority_count > 0,
            "retracted": retracted,
            "title_mismatch": bool(titles) and not any(_text_matches(source.get("title"), title, floor=0.6) for title in titles),
            "evidence_mismatch": bool(evidence and abstracts)
            and not any(_text_matches(value, abstract, floor=0.35) for value in evidence for abstract in abstracts),
        }

    try:
        with httpx.Client(
            timeout=_timeout_s(),
            follow_redirects=True,
            headers={"User-Agent": "Researka/1.0 (https://researka.org)"},
        ) as client:
            with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
                results = list(pool.map(lambda item: check(client, item), candidates))
    except Exception as exc:
        log.warning("source_metadata_unavailable", extra={"error": str(exc)})
        results = [{"identity": identity[0], "checked": False} for _, identity in candidates]

    checked = [row["identity"] for row in results if row.get("checked")]
    unverified = [row["identity"] for row in results if not row.get("checked")]
    retracted = [row["identity"] for row in results if row.get("retracted")]
    title_mismatches = [row["identity"] for row in results if row.get("title_mismatch")]
    evidence_mismatches = [row["identity"] for row in results if row.get("evidence_mismatch")]
    blocked = retracted or title_mismatches
    uncertain = evidence_mismatches or unverified
    recommendation = "reject" if blocked else _metadata_unavailable_recommendation() if uncertain else "pass"
    return {
        "available": not unverified,
        "recommendation": recommendation,
        "checked": checked,
        "unverified": unverified,
        "retracted": retracted,
        "title_mismatches": title_mismatches,
        "evidence_mismatches": evidence_mismatches,
    }


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
