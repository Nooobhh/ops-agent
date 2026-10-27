"""Process-local persistent HTTP sessions for trusted internal JSON APIs."""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


CONNECT_TIMEOUT_SECONDS = 3.0
CONNECT_RETRIES = 2


class PersistentJsonClient:
    def __init__(self) -> None:
        session = requests.Session()
        session.trust_env = False
        retry = Retry(
            total=CONNECT_RETRIES,
            connect=CONNECT_RETRIES,
            read=0,
            redirect=0,
            status=0,
            backoff_factor=0.1,
            allowed_methods=frozenset({"GET"}),
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=4,
            pool_maxsize=8,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self.session = session
        self._lock = threading.RLock()

    def get_json(
        self,
        url: str,
        *,
        auth: Optional[Tuple[str, str]] = None,
        timeout_seconds: float = 30.0,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        timeout = float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        with self._lock:
            response = self.session.get(
                url,
                auth=auth,
                headers=dict(headers or {}),
                timeout=(min(CONNECT_TIMEOUT_SECONDS, timeout), timeout),
            )
            if response.status_code != 200:
                raise ValueError(f"http status {response.status_code}")
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON response must be an object")
        return payload


_CLIENTS: Dict[Tuple[str, str], PersistentJsonClient] = {}
_CLIENTS_LOCK = threading.Lock()


def persistent_json_get(
    url: str,
    auth: Optional[Tuple[str, str]] = None,
    *,
    timeout_seconds: float = 30.0,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Reuse one connection pool per origin in the current process thread."""
    parsed = urlsplit(str(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid internal HTTP URL: {url}")
    origin = (parsed.scheme, parsed.netloc)
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(origin)
        if client is None:
            client = PersistentJsonClient()
            _CLIENTS[origin] = client
    return client.get_json(
        url,
        auth=auth,
        timeout_seconds=timeout_seconds,
        headers=headers,
    )
