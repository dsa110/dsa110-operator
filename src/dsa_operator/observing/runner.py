"""Plan runner — turns the active observing plan into pointing actions.

Every tick it asks the plan what declination the array should be at *now*,
compares it to the currently-commanded dec (from ``/mon/array/dec``), and
if they differ by more than a tolerance issues a ``point_array`` —
**through the ControlEngine**. That means a plan-driven move obeys exactly
the same gauntlet as a manual one: it needs the executor lease, isn't
paused, passes the gate (so during commissioning each move still requires
human approval), and is shadow unless ``point_array`` is promoted to live.

The runner has no autonomy of its own: it never calls an executor, never
writes etcd. It only proposes the next pointing and hands it to the engine.
A background loop that calls :meth:`apply` on a cadence is Phase 5; here
the method is explicit and testable.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from dsa_operator.control.engine import ControlEngine, Decision, Outcome
from dsa_operator.observing.plan import PlanStore

LOG = logging.getLogger("dsa_operator.observing.runner")


@dataclass
class TickResult:
    moved: bool
    reason: str
    target_dec: Optional[float] = None
    current_dec: Optional[float] = None
    segment_label: str = ""
    decision: Optional[Decision] = None

    def to_json(self) -> dict[str, Any]:
        d = {
            "moved": self.moved,
            "reason": self.reason,
            "target_dec": self.target_dec,
            "current_dec": self.current_dec,
            "segment_label": self.segment_label,
        }
        if self.decision is not None:
            d["decision"] = self.decision.to_json()
        return d


class PlanRunner:
    def __init__(
        self, engine: ControlEngine, plan_store: PlanStore, read_etcd: Any, *,
        actor: str, session_id: str, dec_tol_deg: float = 0.25,
        now=time.time,
    ) -> None:
        self._engine = engine
        self._plans = plan_store
        self._read = read_etcd
        self.actor = actor
        self.session_id = session_id
        self.dec_tol = float(dec_tol_deg)
        self._now = now

    def current_commanded_dec(self) -> Optional[float]:
        d = self._read.get_dict("/mon/array/dec")
        if isinstance(d, dict) and "dec_deg" in d:
            try:
                return float(d["dec_deg"])
            except (TypeError, ValueError):
                return None
        return None

    def decide(self, now: Optional[float] = None) -> TickResult:
        """What the runner *would* do, without touching the engine."""
        t = now if now is not None else self._now()
        plan = self._plans.get()
        if plan is None:
            return TickResult(False, "no active plan")
        seg = plan.active_at(t)
        if seg is None:
            nxt = plan.next_segment(t)
            reason = "no active segment now"
            if nxt is not None:
                reason += f"; next {nxt.label or 'segment'} in {nxt.t_start - t:.0f}s"
            return TickResult(False, reason)
        cur = self.current_commanded_dec()
        target = seg.dec_deg
        if cur is not None and abs(cur - target) <= self.dec_tol:
            return TickResult(False, "already on target", target_dec=target,
                              current_dec=cur, segment_label=seg.label)
        return TickResult(True, "move required", target_dec=target,
                          current_dec=cur, segment_label=seg.label)

    def apply(self, now: Optional[float] = None) -> TickResult:
        """Execute one tick: issue point_array through the engine if needed."""
        res = self.decide(now)
        if not res.moved:
            return res
        decision = self._engine.evaluate(
            "point_array", {"dec_deg": res.target_dec},
            actor=self.actor, session_id=self.session_id,
        )
        res.decision = decision
        # 'moved' reflects whether a control path was taken; the decision's
        # outcome says whether it was executed / shadowed / needs approval.
        res.reason = f"point_array -> {decision.outcome.value}"
        if decision.outcome not in (Outcome.EXECUTED, Outcome.SHADOW):
            res.moved = False
        return res


__all__ = ["PlanRunner", "TickResult"]
