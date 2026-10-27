"""Runtime network environment helpers."""
from __future__ import annotations

import os
from urllib.parse import urlsplit


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def configure_direct_network(*, environ=None) -> None:
    """Force this process and inherited child processes to connect directly."""
    target = os.environ if environ is None else environ
    for key in _PROXY_ENV_KEYS:
        target.pop(key, None)
    target["NO_PROXY"] = "*"
    target["no_proxy"] = "*"


def configure_no_proxy(values, *, environ=None) -> None:
    """Merge configured endpoint hosts into NO_PROXY."""
    target = os.environ if environ is None else environ
    merged = []
    for key in ("NO_PROXY", "no_proxy"):
        for item in str(target.get(key, "")).split(","):
            value = item.strip()
            if value and value not in merged:
                merged.append(value)
    for item in values:
        value = str(item or "").strip()
        if not value:
            continue
        parsed = urlsplit(value if "://" in value else f"//{value}")
        host = parsed.hostname or value
        if host not in merged:
            merged.append(host)
    rendered = ",".join(merged)
    target["NO_PROXY"] = rendered
    target["no_proxy"] = rendered
