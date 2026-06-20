"""Egress allowlist enforcement.

`config/egress_allowlist.yaml` declares the ONLY outbound hosts the operator
may contact (Anthropic, Slack, and SSH to h23). This module turns that
document into runtime teeth:

* :func:`host_allowed` / :func:`assert_url_allowed` — application-level checks
  used at egress call sites (e.g. the Slack poster).
* :func:`install_socket_guard` — an opt-in DNS tripwire that wraps
  ``socket.getaddrinfo`` so any attempt to resolve a non-allowlisted public
  host fails closed, regardless of which HTTP library makes it (requests,
  httpx/anthropic, urllib). Loopback + private addresses are always allowed
  so the SSH-forwarded etcd/dashboard ports keep working. Enabled when
  ``DSA_OPERATOR_ENFORCE_EGRESS`` is truthy (the durable enforcement is still
  the host firewall; this is defense-in-depth that travels with the code).

Fail-closed by default for *public* hosts; fail-open only for loopback and
RFC-1918 private ranges (the tunnel).
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("dsa_operator.audit.egress")

_CONFIG = Path(__file__).resolve().parents[3] / "config" / "egress_allowlist.yaml"


class EgressError(RuntimeError):
    """Raised when egress to a non-allowlisted host is attempted."""


def load_allowlist(path: Optional[str | Path] = None) -> dict:
    import yaml
    p = Path(path) if path is not None else _CONFIG
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def allowed_hosts(allowlist: Optional[dict] = None) -> frozenset[str]:
    """Flatten the configured hostnames (transport: ssh entries excluded —
    those are reached over SSH, not direct sockets from Python)."""
    doc = allowlist if allowlist is not None else load_allowlist()
    hosts: set[str] = set()
    for entry in (doc.get("allow", {}) or {}).values():
        if not isinstance(entry, dict):
            continue
        if entry.get("transport") == "ssh":
            continue
        for h in entry.get("hosts", []) or []:
            hosts.add(str(h).lower())
    return frozenset(hosts)


def _is_private(host: str) -> bool:
    if host in ("localhost",) or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


def host_allowed(host: str, allowlist: Optional[dict] = None) -> bool:
    """True if ``host`` is loopback/private or an exact allowlisted host."""
    if not host:
        return False
    h = host.lower().rstrip(".")
    if _is_private(h):
        return True
    return h in allowed_hosts(allowlist)


def assert_url_allowed(url: str, allowlist: Optional[dict] = None) -> None:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not host_allowed(host, allowlist):
        raise EgressError(f"egress to {host!r} is not on the allowlist")


# -- socket-level tripwire ---------------------------------------------------
_GUARD_LOCK = threading.Lock()
_ORIG_GETADDRINFO = None


def install_socket_guard(allowlist: Optional[dict] = None) -> bool:
    """Wrap ``socket.getaddrinfo`` to block non-allowlisted public hosts.

    Idempotent; returns True if the guard is (now) installed. The host
    firewall remains the primary control — this catches code-path mistakes.
    """
    global _ORIG_GETADDRINFO
    allowed = set(allowed_hosts(allowlist))
    # When the operator reaches etcd directly (e.g. running ON h23 with
    # DSA_OPERATOR_ETCD_HOST=etcdv3service.pro.pvt), that internal host is a
    # legitimate, required endpoint — allow it so enforcement can stay on there.
    etcd_host = os.environ.get("DSA_OPERATOR_ETCD_HOST", "").strip().lower()
    if etcd_host:
        allowed.add(etcd_host)
    allowed = frozenset(allowed)
    with _GUARD_LOCK:
        if _ORIG_GETADDRINFO is not None:
            return True
        _ORIG_GETADDRINFO = socket.getaddrinfo
        orig = _ORIG_GETADDRINFO

        def guarded(host, *args, **kwargs):
            h = str(host).lower().rstrip(".") if host else ""
            if not (_is_private(h) or h in allowed):
                LOG.error("egress BLOCKED: %s not on allowlist", h)
                raise EgressError(f"egress to {h!r} is not on the allowlist")
            return orig(host, *args, **kwargs)

        socket.getaddrinfo = guarded                       # type: ignore[assignment]
    LOG.info("egress socket guard installed (allow: %s + loopback/private)",
             ", ".join(sorted(allowed)) or "<none>")
    return True


def uninstall_socket_guard() -> None:
    global _ORIG_GETADDRINFO
    with _GUARD_LOCK:
        if _ORIG_GETADDRINFO is not None:
            socket.getaddrinfo = _ORIG_GETADDRINFO         # type: ignore[assignment]
            _ORIG_GETADDRINFO = None


def maybe_install_from_env() -> bool:
    """Install the guard iff DSA_OPERATOR_ENFORCE_EGRESS is truthy."""
    if str(os.environ.get("DSA_OPERATOR_ENFORCE_EGRESS", "")).lower() in (
            "1", "true", "yes", "on"):
        return install_socket_guard()
    return False


__all__ = [
    "EgressError", "load_allowlist", "allowed_hosts", "host_allowed",
    "assert_url_allowed", "install_socket_guard", "uninstall_socket_guard",
    "maybe_install_from_env",
]
