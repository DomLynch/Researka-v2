from __future__ import annotations

import os
from urllib.parse import urlparse


def validated_service_url(url: str, *, label: str) -> str:
    value = url.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"invalid_{label}_url")
    if os.getenv("RESEARKA_V2_ENV", "development").strip().lower() == "production" and parsed.scheme != "https":
        raise RuntimeError(f"insecure_{label}_url")
    return value
