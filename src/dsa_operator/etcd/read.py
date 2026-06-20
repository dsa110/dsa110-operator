"""Read-only etcd client.

DSA-110 stores everything as JSON dicts under keys like ``/mon/corr_rt/3``
(see dsa110-rt ``dsautils.dsa_store.DsaStore``). This module provides the
minimal read surface the operator needs — ``get(key)`` and
``get_prefix(prefix)`` — with JSON decoding, against the etcd port the SSH
tunnel forwards to ``127.0.0.1:12379``.

It is **read-only by construction**: there is no put/delete/lease/watch
method here. The single-executor lease (Phase 2) and any write path live
in separate, policy-gated modules so that nothing in the Phase-0 import
graph can mutate observatory state.

The backend is abstracted behind :class:`EtcdReader` (a ``Protocol``) so
tests inject :class:`FakeEtcdReader` and we never need a live cluster.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable, Optional, Protocol, runtime_checkable

from dsa_operator import DEFAULT_LOCAL_ETCD_PORT

LOG = logging.getLogger("dsa_operator.etcd")


@runtime_checkable
class EtcdReader(Protocol):
    """Minimal raw key/value read surface (bytes in, bytes out)."""

    def get(self, key: str) -> Optional[bytes]:
        ...

    def get_prefix(self, prefix: str) -> Iterable[tuple[str, bytes]]:
        """Yield ``(key, raw_value)`` for every key under ``prefix``."""
        ...


class _Etcd3Reader:
    """Thin wrapper over a real ``etcd3`` client, read methods only."""

    def __init__(self, host: str, port: int) -> None:
        # etcd3's generated protobuf code predates its bundled protobuf runtime;
        # the pure-python impl avoids "Descriptors cannot be created directly".
        os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
        import etcd3  # lazy: only needed for the live backend

        self._c = etcd3.client(host=host, port=port)

    def get(self, key: str) -> Optional[bytes]:
        value, _meta = self._c.get(key)
        return value

    def get_prefix(self, prefix: str) -> Iterable[tuple[str, bytes]]:
        for value, meta in self._c.get_prefix(prefix):
            yield meta.key.decode("utf-8", "replace"), value


class FakeEtcdReader:
    """In-memory reader for tests. Maps ``key -> dict`` (JSON-encoded)."""

    def __init__(self, data: Optional[dict[str, Any]] = None) -> None:
        # Store raw bytes so the decode path is exercised identically.
        self._raw: dict[str, bytes] = {}
        for k, v in (data or {}).items():
            self.set(k, v)

    def set(self, key: str, value: Any) -> None:
        if isinstance(value, (bytes, bytearray)):
            self._raw[key] = bytes(value)
        else:
            self._raw[key] = json.dumps(value).encode("utf-8")

    def get(self, key: str) -> Optional[bytes]:
        return self._raw.get(key)

    def get_prefix(self, prefix: str) -> Iterable[tuple[str, bytes]]:
        for k in sorted(self._raw):
            if k.startswith(prefix):
                yield k, self._raw[k]


def _decode_json(raw: Optional[bytes], key: str) -> Optional[Any]:
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        LOG.warning("etcd value at %s is not JSON; returning raw string", key)
        try:
            return raw.decode("utf-8", "replace")
        except Exception:                                  # noqa: BLE001
            return None


class ReadOnlyEtcd:
    """JSON-decoding, read-only facade over an :class:`EtcdReader`."""

    def __init__(self, reader: EtcdReader) -> None:
        self._reader = reader

    def get_dict(self, key: str) -> Optional[Any]:
        """Return the JSON-decoded value at ``key`` (or None if absent)."""
        return _decode_json(self._reader.get(key), key)

    def get_prefix_dict(self, prefix: str) -> dict[str, Any]:
        """Return ``{key: decoded_value}`` for everything under ``prefix``."""
        out: dict[str, Any] = {}
        for key, raw in self._reader.get_prefix(prefix):
            out[key] = _decode_json(raw, key)
        return out

    def keys(self, prefix: str) -> list[str]:
        return [k for k, _ in self._reader.get_prefix(prefix)]


def connect_readonly(
    *, host: str = "127.0.0.1", port: int = DEFAULT_LOCAL_ETCD_PORT,
) -> ReadOnlyEtcd:
    """Connect to the (tunnel-forwarded) etcd as a read-only facade.

    Defaults to the loopback port the SSH tunnel forwards etcd to.
    """
    return ReadOnlyEtcd(_Etcd3Reader(host, port))


__all__ = [
    "EtcdReader",
    "FakeEtcdReader",
    "ReadOnlyEtcd",
    "connect_readonly",
]
