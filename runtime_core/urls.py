from __future__ import annotations

import os
from ipaddress import ip_address
from urllib.parse import urlparse


def _is_loopback(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def validated_service_url(url: str, *, label: str) -> str:
    value = url.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"invalid_{label}_url")
    if (
        os.getenv("RESEARKA_V2_ENV", "development").strip().lower() == "production"
        and parsed.scheme != "https"
        and not _is_loopback(parsed.hostname)
    ):
        raise RuntimeError(f"insecure_{label}_url")
    return value
