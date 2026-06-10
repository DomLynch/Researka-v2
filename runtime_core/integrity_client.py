from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)


def _base_url() -> str:
    return os.getenv("RESEARKA_INTEGRITY_URL", "https://integrity.researka.org").rstrip("/")


def _enabled() -> bool:
    return os.getenv("RESEARKA_INTEGRITY_ENABLED", "1") == "1"


def _timeout_s() -> float:
    try:
        return float(os.getenv("RESEARKA_INTEGRITY_TIMEOUT_S", "3"))
    except ValueError:
        return 3.0


def _headers() -> dict[str, str]:
    api_key = os.getenv("RESEARKA_INTEGRITY_API_KEY")
    return {"x-api-key": api_key} if api_key else {}


def _unavailable_recommendation() -> str:
    # When the enabled integrity service is unreachable we never silently pass:
    # the result is always stamped available=False. RESEARKA_INTEGRITY_FAIL_CLOSED=1
    # additionally holds the submission (revise) instead of letting it proceed.
    return "revise" if os.getenv("RESEARKA_INTEGRITY_FAIL_CLOSED", "0") == "1" else "pass"


def check_integrity(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not _enabled():
        return None
    try:
        with httpx.Client(timeout=_timeout_s()) as client:
            response = client.post(f"{_base_url()}/check", json=payload, headers=_headers())
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict):
                return result
            raise ValueError("integrity service returned a non-object response")
    except Exception as exc:
        log.warning("integrity_check_unavailable", extra={"error": str(exc)})
        return {
            "available": False,
            "recommendation": _unavailable_recommendation(),
            "reason": f"integrity_unavailable: {exc}",
        }


def index_integrity(payload: dict[str, Any]) -> None:
    if not _enabled():
        return
    try:
        with httpx.Client(timeout=_timeout_s()) as client:
            response = client.post(f"{_base_url()}/index", json=payload, headers=_headers())
            if response.status_code == 409:
                log.info("integrity_index_duplicate", extra={"response": response.text})
                return
            response.raise_for_status()
    except Exception as exc:
        log.warning("integrity_index_fail_open", extra={"error": str(exc)})
