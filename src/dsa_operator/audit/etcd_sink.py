"""Mirror audit rows into the shared etcd ``/operator/audit/`` trail.

The local JSONL file is the durable system of record; this sink adds a
cluster-visible copy so anyone watching etcd (or a future dashboard panel)
sees operator activity. It writes only under ``/operator/audit/`` via the
prefix-guarded :class:`~dsa_operator.etcd.write.OperatorEtcdWriter`, so it
inherits the "cannot touch a control key" guarantee.

Failures are swallowed by :class:`~dsa_operator.audit.log.AuditLog` (the
sink is best-effort; losing the etcd mirror must never break the local
write).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from dsa_operator.etcd.write import OperatorEtcdWriter

AUDIT_PREFIX = "/operator/audit/"


class EtcdAuditSink:
    def __init__(self, writer: OperatorEtcdWriter,
                 *, ttl_s: Optional[int] = 30 * 24 * 3600) -> None:
        self._w = writer
        self._ttl = ttl_s
        self._lease_id: Optional[int] = None

    def _lease(self) -> Optional[int]:
        if self._ttl is None:
            return None
        # One shared, periodically-refreshed lease keeps the trail bounded.
        if self._lease_id is None:
            self._lease_id = self._w.grant_lease(self._ttl)
        return self._lease_id

    def __call__(self, payload: dict[str, Any]) -> None:
        ts = float(payload.get("ts", time.time()))
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
        key = f"{AUDIT_PREFIX}{day}/{int(ts * 1e6)}"
        self._w.put(key, payload, lease_id=self._lease())


__all__ = ["EtcdAuditSink", "AUDIT_PREFIX"]
