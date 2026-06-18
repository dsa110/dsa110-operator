"""Read-only client for the h23 ``dsa_monitor`` dashboard (JSON endpoints).

Reached over the SSH tunnel at ``127.0.0.1:15778``. Phase 0 only issues
``GET`` requests to the dashboard's JSON endpoints (status / snapshots /
recent events). We never POST here — the dashboard's POST verbs are the
control surface and arrive (gated, in shadow first) in later phases.

The client is pinned to loopback: it refuses any base URL that isn't a
local forwarded port, so a stray config can't make it talk to an
arbitrary host (egress allowlist, by construction).
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Protocol
from urllib.parse import urlparse

from dsa_operator import DEFAULT_LOCAL_DASHBOARD_PORT

LOG = logging.getLogger("dsa_operator.dashboard")

_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class HttpGetter(Protocol):
    def __call__(self, url: str, timeout: float) -> dict[str, Any]:
        ...


def _requests_get_json(url: str, timeout: float) -> dict[str, Any]:
    import requests  # lazy

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


class DashboardClient:
    """Loopback-pinned GET-only dashboard reader."""

    def __init__(
        self,
        port: int = DEFAULT_LOCAL_DASHBOARD_PORT,
        *,
        host: str = "127.0.0.1",
        timeout_s: float = 8.0,
        getter: Optional[HttpGetter] = None,
    ) -> None:
        if host not in _ALLOWED_HOSTS:
            raise ValueError(
                f"dashboard host {host!r} is not loopback; the dashboard is "
                f"only reachable via the SSH tunnel's local forward"
            )
        self.base = f"http://{host}:{int(port)}"
        self.timeout_s = timeout_s
        self._get = getter or _requests_get_json

    def get(self, path: str) -> dict[str, Any]:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base + path
        parsed = urlparse(url)
        if parsed.hostname not in _ALLOWED_HOSTS:
            raise ValueError(f"refusing non-loopback dashboard URL: {url}")
        return self._get(url, self.timeout_s)


__all__ = ["DashboardClient", "HttpGetter"]
