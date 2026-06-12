from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any

import httpx

log = logging.getLogger(__name__)

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
