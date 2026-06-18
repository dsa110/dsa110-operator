"""Single-executor arbitration via an etcd lease.

Many people may watch and ask questions; only **one** session at a time
may execute (or, in this build, *shadow-execute*) control actions. That
right is held as a short-TTL etcd lease on a single key under
``/operator/``. Acquisition is a compare-and-create txn, so the race is
resolved by etcd itself — there is exactly one holder.

* ``acquire``  — create the holder key bound to a fresh lease (idempotent
  for the same session, which just refreshes).
* ``refresh``  — keepalive; the caller (web app / monitor loop) pings on a
  cadence well under the TTL.
* ``release``  — revoke the lease; the key vanishes immediately.
* ``takeover`` — explicit, audited seizure: revoke the incumbent's lease,
  then acquire. Used when an operator must wrest control from a stale or
  away session.

If a holder stops refreshing (crash, network drop, laptop sleep), the
lease expires and the right is free again — no manual cleanup.
"""
from __future__ import annotations

import logging
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from dsa_operator.etcd.write import OperatorEtcdWriter

LOG = logging.getLogger("dsa_operator.control.lease")

LEASE_KEY = "/operator/executor/holder"
DEFAULT_TTL_S = 30


@dataclass(frozen=True)
class LeaseHolder:
    actor: str
    session_id: str
    host: str
    since: float
    lease_id: int = 0

    def to_json(self) -> dict:
        return {
            "actor": self.actor,
            "session_id": self.session_id,
            "host": self.host,
            "since": self.since,
            "lease_id": self.lease_id,
        }


class ExecutorLease:
    def __init__(
        self,
        writer: OperatorEtcdWriter,
        *,
        ttl_s: int = DEFAULT_TTL_S,
        host: Optional[str] = None,
        now=time.time,
    ) -> None:
        self._w = writer
        self._ttl = int(ttl_s)
        self._host = host or socket.gethostname()
        self._now = now
        self._my_lease_id: Optional[int] = None
        self._my_session: Optional[str] = None

    # -- observation ----------------------------------------------------------
    def holder(self) -> Optional[LeaseHolder]:
        got = self._w.get(LEASE_KEY)
        if got is None:
            return None
        value, lease_id = got
        if not isinstance(value, dict):
            return None
        return LeaseHolder(
            actor=value.get("actor", "?"),
            session_id=value.get("session_id", "?"),
            host=value.get("host", "?"),
            since=float(value.get("since", 0.0)),
            lease_id=lease_id or int(value.get("lease_id", 0)),
        )

    def held_by(self, session_id: str) -> bool:
        h = self.holder()
        return h is not None and h.session_id == session_id

    # -- mutation -------------------------------------------------------------
    def acquire(self, actor: str, session_id: str) -> bool:
        """Try to become the executor. Idempotent for the same session."""
        h = self.holder()
        if h is not None:
            if h.session_id == session_id:
                self.refresh()
                return True
            return False
        lease_id = self._w.grant_lease(self._ttl)
        value = LeaseHolder(actor, session_id, self._host, self._now(),
                            lease_id).to_json()
        ok = self._w.create_if_absent(LEASE_KEY, value, lease_id)
        if ok:
            self._my_lease_id = lease_id
            self._my_session = session_id
        else:
            # Lost the race; drop the lease we just minted.
            self._w.revoke_lease(lease_id)
        return ok

    def refresh(self) -> bool:
        if self._my_lease_id is None:
            return False
        self._w.refresh_lease(self._my_lease_id)
        return True

    def release(self, session_id: Optional[str] = None) -> bool:
        h = self.holder()
        if h is None:
            return False
        if session_id is not None and h.session_id != session_id:
            return False
        self._w.revoke_lease(h.lease_id)
        # delete is belt-and-braces; revoke already drops the lease-bound key
        try:
            self._w.delete(LEASE_KEY)
        except Exception:                                  # noqa: BLE001
            pass
        if session_id is None or session_id == self._my_session:
            self._my_lease_id = None
            self._my_session = None
        return True

    def takeover(self, actor: str, session_id: str) -> bool:
        """Forcibly seize the lease. The caller must audit this."""
        h = self.holder()
        if h is not None:
            self._w.revoke_lease(h.lease_id)
            try:
                self._w.delete(LEASE_KEY)
            except Exception:                              # noqa: BLE001
                pass
        return self.acquire(actor, session_id)


def new_session_id() -> str:
    return uuid.uuid4().hex


__all__ = ["ExecutorLease", "LeaseHolder", "LEASE_KEY", "DEFAULT_TTL_S",
           "new_session_id"]
