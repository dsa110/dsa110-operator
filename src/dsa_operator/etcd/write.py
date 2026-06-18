"""Narrowly-scoped etcd WRITE path for operator coordination only.

Phase 2 introduces the first writes to etcd — but **only** for the
operator's own coordination namespace: the single-executor lease and the
shared audit trail, all under the ``/operator/`` prefix. This client
*physically cannot* write a real control key (``/cmd/...``, ``/cnf/...``):
:meth:`OperatorEtcdWriter.put` refuses any key outside ``/operator/``.

That keeps the strong Phase-0/1 invariant — the model never holds a raw
mutating client, and nothing in this build can move the array — while
still letting the operator arbitrate who *would* be the executor.

The lease/txn surface is abstracted behind :class:`OperatorBackend` so
tests run against :class:`FakeOperatorBackend` (in-memory, deterministic
clock) with no live etcd.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional, Protocol

LOG = logging.getLogger("dsa_operator.etcd.write")

OPERATOR_PREFIX = "/operator/"


class OperatorBackend(Protocol):
    """Minimal lease + create-if-absent + put/get/delete over raw bytes."""

    def grant_lease(self, ttl_s: int) -> int: ...
    def refresh_lease(self, lease_id: int) -> None: ...
    def revoke_lease(self, lease_id: int) -> None: ...
    def create_if_absent(self, key: str, value: bytes, lease_id: int) -> bool: ...
    def get(self, key: str) -> Optional[tuple[bytes, int]]: ...   # (value, lease_id)
    def put(self, key: str, value: bytes, lease_id: Optional[int] = None) -> None: ...
    def delete(self, key: str) -> None: ...


def _check_prefix(key: str) -> None:
    if not key.startswith(OPERATOR_PREFIX):
        raise ValueError(
            f"refusing to write etcd key {key!r}: operator writes are confined "
            f"to {OPERATOR_PREFIX!r} (control keys are out of reach in this build)"
        )


class OperatorEtcdWriter:
    """Prefix-guarded facade over an :class:`OperatorBackend`.

    Every mutating method enforces the ``/operator/`` prefix, so even a
    bug can't escape the coordination namespace.
    """

    def __init__(self, backend: OperatorBackend) -> None:
        self._b = backend

    # lease ops ---------------------------------------------------------------
    def grant_lease(self, ttl_s: int) -> int:
        return self._b.grant_lease(int(ttl_s))

    def refresh_lease(self, lease_id: int) -> None:
        self._b.refresh_lease(lease_id)

    def revoke_lease(self, lease_id: int) -> None:
        self._b.revoke_lease(lease_id)

    def create_if_absent(self, key: str, value: Any, lease_id: int) -> bool:
        _check_prefix(key)
        return self._b.create_if_absent(key, _enc(value), lease_id)

    def put(self, key: str, value: Any, lease_id: Optional[int] = None) -> None:
        _check_prefix(key)
        self._b.put(key, _enc(value), lease_id)

    def get(self, key: str) -> Optional[tuple[Any, int]]:
        got = self._b.get(key)
        if got is None:
            return None
        raw, lease_id = got
        return _dec(raw), lease_id

    def delete(self, key: str) -> None:
        _check_prefix(key)
        self._b.delete(key)


def _enc(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")


def _dec(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


# --- real etcd3 backend ----------------------------------------------------

class Etcd3Backend:
    """Lease + txn-create backed by a real ``etcd3`` client."""

    def __init__(self, host: str, port: int) -> None:
        import etcd3  # lazy

        self._c = etcd3.client(host=host, port=port)
        self._leases: dict[int, Any] = {}

    def grant_lease(self, ttl_s: int) -> int:
        lease = self._c.lease(ttl_s)
        self._leases[lease.id] = lease
        return lease.id

    def refresh_lease(self, lease_id: int) -> None:
        lease = self._leases.get(lease_id)
        if lease is not None:
            lease.refresh()

    def revoke_lease(self, lease_id: int) -> None:
        try:
            self._c.revoke_lease(lease_id)
        finally:
            self._leases.pop(lease_id, None)

    def create_if_absent(self, key: str, value: bytes, lease_id: int) -> bool:
        txn = self._c.transactions
        ok, _ = self._c.transaction(
            compare=[txn.version(key) == 0],
            success=[txn.put(key, value, lease=lease_id)],
            failure=[],
        )
        return bool(ok)

    def get(self, key: str) -> Optional[tuple[bytes, int]]:
        value, meta = self._c.get(key)
        if value is None:
            return None
        lease_id = int(getattr(meta, "lease_id", 0) or getattr(meta, "lease", 0) or 0)
        return value, lease_id

    def put(self, key: str, value: bytes, lease_id: Optional[int] = None) -> None:
        if lease_id:
            self._c.put(key, value, lease=lease_id)
        else:
            self._c.put(key, value)

    def delete(self, key: str) -> None:
        self._c.delete(key)


def connect_writer(*, host: str = "127.0.0.1",
                   port: int = 12379) -> OperatorEtcdWriter:
    return OperatorEtcdWriter(Etcd3Backend(host, port))


# --- fake backend for tests ------------------------------------------------

class FakeOperatorBackend:
    """In-memory backend with a manual clock and lease expiry.

    ``now`` is a callable so tests can advance time and observe lease
    expiry deterministically.
    """

    def __init__(self, now: Optional[Any] = None) -> None:
        self._now = now or time.time
        self._next_lease = 1000
        self._lease_expiry: dict[int, float] = {}
        self._kv: dict[str, tuple[bytes, int]] = {}   # key -> (value, lease_id)

    # internal: drop keys whose lease has expired
    def _gc(self) -> None:
        t = self._now()
        dead = {lid for lid, exp in self._lease_expiry.items() if exp <= t}
        for lid in dead:
            self._lease_expiry.pop(lid, None)
        if dead:
            for k in [k for k, (_, lid) in self._kv.items() if lid in dead]:
                del self._kv[k]

    def grant_lease(self, ttl_s: int) -> int:
        self._next_lease += 1
        lid = self._next_lease
        self._lease_expiry[lid] = self._now() + ttl_s
        self._ttl: dict[int, int] = getattr(self, "_ttl", {})
        self._ttl[lid] = ttl_s
        return lid

    def refresh_lease(self, lease_id: int) -> None:
        self._gc()
        if lease_id in self._lease_expiry:
            ttl = getattr(self, "_ttl", {}).get(lease_id, 30)
            self._lease_expiry[lease_id] = self._now() + ttl

    def revoke_lease(self, lease_id: int) -> None:
        self._lease_expiry.pop(lease_id, None)
        for k in [k for k, (_, lid) in self._kv.items() if lid == lease_id]:
            del self._kv[k]

    def create_if_absent(self, key: str, value: bytes, lease_id: int) -> bool:
        self._gc()
        if key in self._kv:
            return False
        self._kv[key] = (value, lease_id)
        return True

    def get(self, key: str) -> Optional[tuple[bytes, int]]:
        self._gc()
        return self._kv.get(key)

    def put(self, key: str, value: bytes, lease_id: Optional[int] = None) -> None:
        self._gc()
        self._kv[key] = (value, int(lease_id or 0))

    def delete(self, key: str) -> None:
        self._kv.pop(key, None)


__all__ = [
    "OPERATOR_PREFIX",
    "OperatorBackend",
    "OperatorEtcdWriter",
    "Etcd3Backend",
    "FakeOperatorBackend",
    "connect_writer",
]
