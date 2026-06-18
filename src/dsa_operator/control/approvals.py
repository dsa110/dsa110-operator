"""Human approval grants for gated control actions.

An ``approval``-gated action may only proceed when a matching, unexpired
approval grant exists — a typed confirmation by an authorized human, bound
to their Google SSO identity, with a short TTL (``approval.ttl_seconds``).

* Single-approver actions need one grant. The requester may self-approve
  (the request *is* the human's typed confirmation in the console).
* Two-person actions (e.g. editing the policy) need two **distinct**
  approvers, and the requester may not be one of them.

A grant matches a control request only if the action AND the exact
parameters match (params are normalised + hashed), so an approval for
"point to dec=33" can't be replayed for "dec=71".

State is in-memory: grants are deliberately short-lived and session-scoped;
nothing here survives a restart (which fails safe — pending approvals must
be re-requested).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


def params_hash(action: str, params: dict[str, Any]) -> str:
    blob = json.dumps({"a": action, "p": params or {}}, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class Approval:
    id: str
    action: str
    params: dict[str, Any]
    phash: str
    requested_by: str
    n_required: int
    ttl_s: float
    created_ts: float
    two_person: bool = False
    granted_by: list[str] = field(default_factory=list)

    def is_expired(self, now: float) -> bool:
        return now >= self.created_ts + self.ttl_s

    def is_satisfied(self) -> bool:
        return len(self.granted_by) >= self.n_required

    def to_json(self, now: Optional[float] = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        return {
            "id": self.id,
            "action": self.action,
            "params": self.params,
            "requested_by": self.requested_by,
            "n_required": self.n_required,
            "two_person": self.two_person,
            "granted_by": list(self.granted_by),
            "satisfied": self.is_satisfied(),
            "expired": self.is_expired(now),
            "expires_in_s": max(0.0, self.created_ts + self.ttl_s - now),
        }


class ApprovalError(RuntimeError):
    pass


class ApprovalStore:
    def __init__(self, now=time.time) -> None:
        self._now = now
        self._by_id: dict[str, Approval] = {}
        self._lock = threading.Lock()

    def request(
        self, action: str, params: dict[str, Any], *, requested_by: str,
        n_required: int = 1, ttl_s: float = 300.0, two_person: bool = False,
    ) -> Approval:
        ap = Approval(
            id=uuid.uuid4().hex[:12],
            action=action,
            params=dict(params or {}),
            phash=params_hash(action, params or {}),
            requested_by=requested_by,
            n_required=max(1, int(n_required)),
            ttl_s=float(ttl_s),
            created_ts=self._now(),
            two_person=two_person,
        )
        with self._lock:
            self._by_id[ap.id] = ap
        return ap

    def grant(self, approval_id: str, approver: str) -> Approval:
        with self._lock:
            ap = self._by_id.get(approval_id)
            if ap is None:
                raise ApprovalError("no such approval request")
            if ap.is_expired(self._now()):
                raise ApprovalError("approval request has expired")
            if approver in ap.granted_by:
                raise ApprovalError("this approver already granted")
            if ap.two_person and approver == ap.requested_by:
                raise ApprovalError(
                    "two-person action: the requester cannot approve it")
            ap.granted_by.append(approver)
            return ap

    def find_active(self, action: str, params: dict[str, Any]) -> Optional[Approval]:
        """Return a satisfied, unexpired grant matching action+params."""
        ph = params_hash(action, params or {})
        now = self._now()
        with self._lock:
            for ap in self._by_id.values():
                if (ap.phash == ph and ap.action == action
                        and ap.is_satisfied() and not ap.is_expired(now)):
                    return ap
        return None

    def consume(self, approval_id: str) -> None:
        """Remove a grant after it authorises an action (single-use)."""
        with self._lock:
            self._by_id.pop(approval_id, None)

    def pending(self) -> list[dict[str, Any]]:
        now = self._now()
        with self._lock:
            return [ap.to_json(now) for ap in self._by_id.values()
                    if not ap.is_expired(now)]

    def gc(self) -> int:
        now = self._now()
        with self._lock:
            dead = [k for k, ap in self._by_id.items() if ap.is_expired(now)]
            for k in dead:
                del self._by_id[k]
        return len(dead)


__all__ = ["Approval", "ApprovalStore", "ApprovalError", "params_hash"]
