"""Read-only etcd access over the forwarded local port.

Phase 0 is strictly read-only: this package exposes ``get`` / ``get_prefix``
and never a put/delete/watch-write. The live backend (:mod:`etcd3`) is
imported lazily so the package — and the test suite, which uses
:class:`FakeEtcdReader` — works on a laptop without etcd3 installed.
"""
from __future__ import annotations

from dsa_operator.etcd.read import (
    EtcdReader,
    FakeEtcdReader,
    ReadOnlyEtcd,
    connect_readonly,
)

__all__ = ["EtcdReader", "FakeEtcdReader", "ReadOnlyEtcd", "connect_readonly"]
