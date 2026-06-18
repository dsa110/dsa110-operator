"""Periodic injection health-check (Phase 5).

Fires a synthetic FRB through ``fire_injection`` (via the gate engine) and,
after a delay, verifies it was detected by watching the injection-match
count from :meth:`ReadOnlyTools.query_injections`. A probe that's submitted
but never matched is the canonical "search is silently broken" signal — an
end-to-end pulse test the standing monitor can't get from health rollups.

State machine (single in-flight probe):

    idle --fire()--> pending --(verify_after_s elapses)--> verify() --> idle

The supervisor owns the *cadence* (when to fire). This class owns the
*mechanics* (submit through the engine, capture a baseline match count,
re-read after the delay, decide pass/fail) so it stays small and testable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from dsa_operator.audit.log import AuditRecord
from dsa_operator.control.engine import ControlEngine, Decision, Outcome

# Conservative default probe: modest DM, on-axis, high target SNR so a
# healthy pipeline always recovers it well above threshold.
DEFAULT_PROBE = {
    "dm_pc_cm3": 100.0,
    "l_rad": 0.0,
    "m_rad": 0.0,
    "width_samples": 1,
    "target_snr": 30.0,
    "profile": "boxcar",
}


@dataclass
class _Pending:
    fired_ts: float
    baseline_matches: int
    decision_outcome: str


@dataclass
class InjectionResult:
    ok: bool
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "message": self.message,
                "details": self.details}


class InjectionHealthCheck:
    def __init__(
        self, engine: ControlEngine, tools: Any, audit: Any, *,
        actor: str = "agent", session_id: str = "supervisor",
        verify_after_s: float = 180.0, probe: Optional[dict[str, Any]] = None,
        now=time.time,
    ) -> None:
        self._engine = engine
        self._tools = tools
        self._audit = audit
        self.actor = actor
        self.session_id = session_id
        self.verify_after_s = float(verify_after_s)
        self.probe = dict(probe or DEFAULT_PROBE)
        self._now = now
        self._pending: Optional[_Pending] = None

    @property
    def in_flight(self) -> bool:
        return self._pending is not None

    def _match_count(self) -> int:
        try:
            inj = self._tools.query_injections()
            matches = (inj or {}).get("matches", {})
            return len(matches) if isinstance(matches, dict) else 0
        except Exception:                                      # noqa: BLE001
            return 0

    def fire(self, now: Optional[float] = None) -> Decision:
        """Submit one injection probe through the gate engine."""
        t = now if now is not None else self._now()
        baseline = self._match_count()
        decision = self._engine.evaluate(
            "fire_injection", dict(self.probe),
            actor=self.actor, session_id=self.session_id)
        # Only treat it as in-flight if a control path was actually taken;
        # a DENIED / NEEDS_APPROVAL probe never reaches the pipeline.
        if decision.outcome in (Outcome.EXECUTED, Outcome.SHADOW):
            self._pending = _Pending(t, baseline, decision.outcome.value)
        try:
            self._audit.record(AuditRecord(
                action="injection_health_check.fire", kind="system",
                actor=self.actor, ok=decision.allowed,
                mode=decision.mode, params=dict(self.probe),
                note=f"outcome={decision.outcome.value}"))
        except Exception:                                      # noqa: BLE001
            pass
        return decision

    def due_to_verify(self, now: Optional[float] = None) -> bool:
        if self._pending is None:
            return False
        t = now if now is not None else self._now()
        return (t - self._pending.fired_ts) >= self.verify_after_s

    def verify(self, now: Optional[float] = None) -> InjectionResult:
        """Check whether the in-flight probe was detected; clears state."""
        if self._pending is None:
            return InjectionResult(True, "no_probe", "no probe in flight")
        pend = self._pending
        self._pending = None
        if pend.decision_outcome == Outcome.SHADOW.value:
            res = InjectionResult(
                True, "shadow_probe",
                "probe was shadow-only (not sent); skipping detection check",
                details={"outcome": pend.decision_outcome})
        else:
            after = self._match_count()
            detected = after > pend.baseline_matches
            res = InjectionResult(
                detected,
                "injection_detected" if detected else "injection_missed",
                ("injection probe detected" if detected else
                 "injection probe NOT detected within "
                 f"{self.verify_after_s:.0f}s — search may be impaired"),
                details={"baseline_matches": pend.baseline_matches,
                         "matches_after": after})
        try:
            self._audit.record(AuditRecord(
                action="injection_health_check.verify", kind="system",
                actor=self.actor, ok=res.ok, note=res.message,
                result=res.details))
        except Exception:                                      # noqa: BLE001
            pass
        return res


__all__ = ["InjectionHealthCheck", "InjectionResult", "DEFAULT_PROBE"]
