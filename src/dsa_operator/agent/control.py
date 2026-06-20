"""The agent's *control* surface (Phase 6).

Phase 1–5 gave the Claude brain only read-only tools. This module is the
bridge that lets it actually *act* — propose control actions, request
approvals, and drive the observing plan — **without ever widening the
trust boundary**. Every mutating path here funnels through the existing
:class:`~dsa_operator.control.engine.ControlEngine`, so the agent is bound
by exactly the same gauntlet as a human clicking the console:

* it must hold the executor **lease** (bound to the chat session),
* the dashboard must not have **locked agents out**,
* the **e-stop** must be clear,
* the action's **gate** still applies — an ``approval`` action returns
  ``needs_approval`` and the agent **cannot** grant it (granting is a human
  action in the console; the agent may only *request* one),
* live execution still requires ``mode: live`` **and** per-action promotion.

So handing the model these tools changes *who can ask*, never *what is
allowed*. A prompt-injected or confused agent can't exceed the policy.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from dsa_operator.audit.log import AuditRecord
from dsa_operator.control.engine import ControlEngine, Outcome
from dsa_operator.observing import astro
from dsa_operator.observing.plan import ObservingPlan, PlanError, PlanStore
from dsa_operator.observing.runner import PlanRunner
from dsa_operator.observing.session import (
    DEFAULT_HOLDOFF, ObservingSequencer, ToolsSiteState)

LOG = logging.getLogger("dsa_operator.agent.control")


class ControlToolError(RuntimeError):
    pass


# "until further instructions" — a far-future segment end (≈100 years).
_OPEN_ENDED_S = 100 * 365 * 86400.0


_NEXT_STEP = {
    "not_executor": "Acquire the executor lease in the console (Lease → "
                    "Acquire) for this session, then ask again.",
    "locked_out": "A human has locked agents out from the dsa110-rt "
                  "dashboard; nothing can run until they re-enable it.",
    "executor_pinned": "The dashboard has pinned control to a specific "
                       "person; only they can execute.",
    "paused": "The e-stop is engaged. A human must resume in the console.",
    "forbidden": "This action is forbidden by policy and cannot be run.",
}


class AgentControl:
    """Control + observing-plan tools bound to one chat identity/session.

    All methods are safe to expose to the model: the engine enforces every
    gate, and the only state this writes directly is the operator-namespace
    observing plan (itself lease-gated here).
    """

    def __init__(
        self, engine: ControlEngine, plan_store: PlanStore, read_etcd: Any, *,
        actor: str, session_id: str, now=time.time, tools: Any = None,
    ) -> None:
        self._engine = engine
        self._plans = plan_store
        self._read = read_etcd
        self._tools = tools
        self.actor = actor
        self.session_id = session_id
        self._now = now

    # -- introspection --------------------------------------------------------
    def list_control_actions(self) -> dict[str, Any]:
        """Every control action with its active gate, so the agent knows
        what it can do autonomously vs what needs human approval."""
        out = {}
        for action in sorted(self._engine.policy.actions):
            out[action] = {
                "gate": self._engine.policy.gate_for(action),
                "reversible": self._engine.policy.is_reversible(action),
                "note": self._engine.policy.action_note(action),
            }
        return {"mode": self._engine.policy.mode, "actions": out}

    def lease_status(self) -> dict[str, Any]:
        holder = self._engine.lease.holder()
        auth = self._engine.authority()
        return {
            "holder": holder.to_json() if holder else None,
            "i_hold": bool(holder and holder.session_id == self.session_id),
            "paused": self._engine.is_paused(),
            "authority": {"agents_enabled": auth.agents_enabled,
                          "executor_email": auth.executor_email,
                          "max_obs_seconds": auth.max_obs_seconds},
        }

    # -- the unified control entry point --------------------------------------
    def propose_action(self, action: str, params: Optional[dict[str, Any]] = None
                       ) -> dict[str, Any]:
        """Run a control action through the full gate engine and report the
        decision (denied / needs_approval / shadow / executed)."""
        if not isinstance(action, str) or not action:
            raise ControlToolError("action must be a non-empty string")
        params = dict(params or {})
        decision = self._engine.evaluate(
            action, params, actor=self.actor, session_id=self.session_id)
        d = decision.to_json()
        outcome = decision.outcome
        if outcome is Outcome.DENIED:
            d["next_step"] = _NEXT_STEP.get(
                _reason_code(decision.reason), "Denied; see reason.")
        elif outcome is Outcome.NEEDS_APPROVAL:
            d["next_step"] = ("This needs human approval. Call request_approval "
                              "with the same action+params; an authorized human "
                              "then grants it in the console. You cannot approve "
                              "it yourself.")
        elif outcome is Outcome.SHADOW:
            d["next_step"] = ("Shadow/dry-run only — no state changed (policy "
                              "mode is shadow or this action isn't promoted to "
                              "live).")
        elif outcome is Outcome.EXECUTED:
            d["next_step"] = "Executed live."
        return d

    def request_approval(self, action: str, params: Optional[dict[str, Any]] = None
                         ) -> dict[str, Any]:
        """Register a pending approval a human can grant in the console.

        The agent can *request* but never *grant* — granting is a typed
        human confirmation bound to a Google identity.
        """
        if action not in self._engine.policy.actions:
            raise ControlToolError(f"{action!r} is not a known control action")
        params = dict(params or {})
        ap = self._engine.approvals.request(
            action, params, requested_by=self.actor,
            n_required=self._engine.policy.required_approvers(action),
            ttl_s=self._engine.policy.approval_ttl_s,
            two_person=self._engine.policy.needs_two_person(action))
        self._audit("request_approval", ok=True,
                    params={"action": action, "params": params, "id": ap.id})
        out = ap.to_json(self._now())
        out["next_step"] = ("Tell the user an approval request is pending "
                            f"(id {ap.id}); an authorized human must grant it "
                            "in the console before this can run.")
        return out

    # -- observing plan -------------------------------------------------------
    def _require_lease(self) -> None:
        if not self._engine.lease.held_by(self.session_id):
            raise ControlToolError(
                "you do not hold the executor lease — acquire it first")

    def _envelope_kwargs(self) -> dict[str, float]:
        from dsa_operator.observing import astro
        pt = self._engine.policy.pointing
        return dict(el_min=float(pt.get("el_min_deg", 30.0)),
                    el_max=float(pt.get("el_max_deg", 125.0)),
                    lat_deg=float(pt.get("lat_ovro_deg", astro.OVRO_LAT_DEG)))

    def set_observing_plan(self, *, sources: Optional[list] = None,
                           segments: Optional[list] = None,
                           window_min: float = 30.0, note: str = ""
                           ) -> dict[str, Any]:
        """Install an observing plan (executor only). Provide either
        ``sources`` (transit-centred) or explicit ``segments``."""
        self._require_lease()
        try:
            if sources:
                plan = ObservingPlan.from_sources(
                    sources, after_unix=self._now(), created_by=self.actor,
                    default_window_min=float(window_min), note=str(note))
            elif segments:
                plan = ObservingPlan.from_segments(
                    segments, created_by=self.actor, note=str(note))
            else:
                raise ControlToolError("provide either sources or segments")
            plan.validate(**self._envelope_kwargs())
        except (PlanError, KeyError, ValueError, TypeError) as exc:
            raise ControlToolError(f"invalid plan: {exc}")
        self._plans.set(plan)            # staged unarmed: nothing runs yet
        self._audit("set_observing_plan", ok=True,
                    params={"n_segments": len(plan.segments)})
        return {"ok": True, "armed": False, "n_segments": len(plan.segments),
                "plan": plan.to_json(),
                "next_step": ("Plan STAGED but NOT armed — nothing will move. "
                              "Present the full schedule (each segment's source, "
                              "RA/Dec, dec->el, transit time, and exact start/end "
                              "times) to the user and ask them to confirm. Only "
                              "after they confirm, call arm_observing_plan. You do "
                              "NOT need to confirm each individual command after "
                              "arming.")}

    def observe_at_dec(self, dec_deg: float, *, label: str = "",
                       start_unix: Optional[float] = None,
                       end_unix: Optional[float] = None, note: str = ""
                       ) -> dict[str, Any]:
        """Stage a single open-ended observation at ``dec_deg`` (executor only,
        staged unarmed). Leave ``end_unix`` empty for 'until further
        instructions'. Still requires confirmation + arm before it runs."""
        t0 = float(start_unix) if start_unix is not None else self._now()
        t1 = float(end_unix) if end_unix is not None else t0 + _OPEN_ENDED_S
        return self.set_observing_plan(
            segments=[{"t_start": t0, "t_end": t1, "dec_deg": float(dec_deg),
                       "label": label or f"dec {float(dec_deg):.3f}"}],
            note=note)

    # -- transits / coordinates (read-only; no lease) -------------------------
    def compute_transits(self, sources: list, *, after_unix: Optional[float] = None
                         ) -> dict[str, Any]:
        """For each source {label, ra_deg, dec_deg}, compute its next transit
        time, transit elevation, and whether it is observable. Coordinates are
        supplied by you (look them up); this only does the sidereal/geometry
        math. Use the result to lay out the schedule for the user to confirm."""
        if not isinstance(sources, list) or not sources:
            raise ControlToolError("sources must be a non-empty list")
        t0 = float(after_unix) if after_unix is not None else self._now()
        pt = self._engine.policy.pointing
        lat = float(pt.get("lat_ovro_deg", astro.OVRO_LAT_DEG))
        el_min = float(pt.get("el_min_deg", 30.0))
        el_max = float(pt.get("el_max_deg", 125.0))
        out = []
        import datetime as _dt
        for s in sources:
            try:
                ra = float(s["ra_deg"]); dec = float(s["dec_deg"])
            except (KeyError, TypeError, ValueError):
                raise ControlToolError(
                    "each source needs numeric ra_deg and dec_deg")
            tt = astro.next_transit_unix(ra, t0)
            el = astro.dec_to_el(dec, lat)
            out.append({
                "label": s.get("label", ""),
                "ra_deg": ra, "dec_deg": dec,
                "transit_el_deg": round(el, 3),
                "observable": astro.is_observable(
                    dec, el_min=el_min, el_max=el_max, lat_deg=lat),
                "next_transit_unix": tt,
                "next_transit_utc": _dt.datetime.utcfromtimestamp(tt).isoformat() + "Z",
                "seconds_to_transit": round(tt - t0, 1),
            })
        return {"now_unix": t0, "lat_deg": lat, "sources": out}

    # -- bring-up preview / arming -------------------------------------------
    def _site(self):
        if self._tools is not None:
            return ToolsSiteState(self._tools)
        # degraded fallback (commanded dec from etcd only)
        read = self._read

        class _EtcdSite:
            def commanded_dec(self):
                d = read.get_dict("/mon/array/dec")
                if isinstance(d, dict) and d.get("dec_deg") is not None:
                    try:
                        return float(d["dec_deg"])
                    except (TypeError, ValueError):
                        return None
                return None

            def n_not_settled(self): return None
            def fleet_state(self): return {}
            def fstable_status(self, dec): return {}
        return _EtcdSite()

    def _sequencer(self) -> ObservingSequencer:
        return ObservingSequencer(
            self._engine, self._plans, self._site(),
            actor=self.actor, session_id=self.session_id)

    def preview_observing_plan(self) -> dict[str, Any]:
        """The exact bring-up steps the sequencer would run for each segment of
        the staged plan, from a current state snapshot. Show this to the user
        for confirmation before arming."""
        return self._sequencer().describe_plan(self._now())

    def arm_observing_plan(self) -> dict[str, Any]:
        """Arm the staged plan (executor only). Call this ONLY after you have
        presented the full schedule and the user has explicitly confirmed it.
        Once armed, the sequencer runs the bring-up autonomously."""
        self._require_lease()
        plan = self._plans.arm(by=self.actor, now=self._now())
        if plan is None:
            raise ControlToolError("no staged plan to arm")
        self._audit("arm_observing_plan", ok=True,
                    params={"n_segments": len(plan.segments)})
        return {"ok": True, "armed": True, "armed_by": self.actor,
                "n_segments": len(plan.segments), "plan": plan.to_json()}

    def disarm_observing_plan(self) -> dict[str, Any]:
        self._require_lease()
        plan = self._plans.disarm()
        self._audit("disarm_observing_plan", ok=True)
        return {"ok": True, "armed": False,
                "plan": plan.to_json() if plan else None}

    def run_observing_step(self) -> dict[str, Any]:
        """Advance the armed plan's bring-up by one step (executor only). The
        autonomy supervisor normally does this on a cadence; this lets you nudge
        it manually."""
        self._require_lease()
        return self._sequencer().apply(self._now()).to_json()

    def observing_status(self) -> dict[str, Any]:
        plan = self._plans.get()
        if plan is None:
            return {"plan": None}
        t = self._now()
        active = plan.active_at(t)
        return {"armed": plan.armed, "armed_by": plan.armed_by,
                "n_segments": len(plan.segments),
                "active_now": active.to_json() if active else None,
                "dec_now": plan.dec_at(t)}

    def preview_plan(self) -> dict[str, Any]:
        return self._runner().decide().to_json()

    def tick_plan(self) -> dict[str, Any]:
        """Run one plan step now: issue point_array through the engine if
        the active dec differs from the commanded dec (executor only)."""
        self._require_lease()
        return self._runner().apply().to_json()

    def clear_plan(self) -> dict[str, Any]:
        self._require_lease()
        self._plans.clear()
        self._audit("clear_observing_plan", ok=True)
        return {"ok": True}

    # -- internals ------------------------------------------------------------
    def _runner(self) -> PlanRunner:
        return PlanRunner(self._engine, self._plans, self._read,
                          actor=self.actor, session_id=self.session_id)

    def _audit(self, action: str, *, ok: bool, params: Optional[dict] = None) -> None:
        try:
            self._engine.audit.record(AuditRecord(
                action=action, kind="control", actor=self.actor, ok=ok,
                mode="live", params=params or {}, note="via agent chat"))
        except Exception:                                      # noqa: BLE001
            pass


def _reason_code(reason: str) -> str:
    r = (reason or "").lower()
    if "lease" in r:
        return "not_executor"
    if "locked out" in r:
        return "locked_out"
    if "pinned" in r:
        return "executor_pinned"
    if "paused" in r or "e-stop" in r:
        return "paused"
    if "forbidden" in r:
        return "forbidden"
    return ""


# --- the control tool catalog the agent may call --------------------------

@dataclass(frozen=True)
class ControlToolSpec:
    name: str
    description: str
    invoke: Callable[[AgentControl, dict[str, Any]], Any]
    input_schema: dict[str, Any] = field(default_factory=lambda: {
        "type": "object", "properties": {}, "required": []})


_OBJ = {"type": "object"}
_ARR = {"type": "array", "items": {"type": "object"}}

CONTROL_TOOL_SPECS: list[ControlToolSpec] = [
    ControlToolSpec(
        "list_control_actions",
        "List every control action with its gate (autonomous vs needs "
        "human approval vs forbidden) and whether it's reversible.",
        lambda c, a: c.list_control_actions()),
    ControlToolSpec(
        "lease_status",
        "Who holds the executor lease, whether THIS session holds it, "
        "the e-stop state, and the dashboard authority.",
        lambda c, a: c.lease_status()),
    ControlToolSpec(
        "propose_action",
        "Run a control action through the gate engine. Returns the decision: "
        "denied (e.g. no lease), needs_approval, shadow (dry run), or "
        "executed. Use list_control_actions to see valid action names and "
        "their params (e.g. point_array{dec_deg}, fire_injection{...}, "
        "bounce_search, set_dumps_enabled{enabled,reason}, utc_start/utc_stop).",
        lambda c, a: c.propose_action(a["action"], a.get("params")),
        {"type": "object",
         "properties": {"action": {"type": "string"}, "params": _OBJ},
         "required": ["action"]}),
    ControlToolSpec(
        "request_approval",
        "Register a pending approval (same action+params) for a human to "
        "grant in the console. You can request but never grant.",
        lambda c, a: c.request_approval(a["action"], a.get("params")),
        {"type": "object",
         "properties": {"action": {"type": "string"}, "params": _OBJ},
         "required": ["action"]}),
    ControlToolSpec(
        "compute_transits",
        "Sidereal/geometry math for a list of sources [{label,ra_deg,dec_deg}] "
        "YOU supply (look the coordinates up): next transit time (UTC), transit "
        "elevation, observability. Use it to lay out a schedule to confirm.",
        lambda c, a: c.compute_transits(a["sources"], after_unix=a.get("after_unix")),
        {"type": "object",
         "properties": {"sources": _ARR, "after_unix": {"type": "number"}},
         "required": ["sources"]}),
    ControlToolSpec(
        "set_observing_plan",
        "STAGE an observing plan UNARMED (executor only; nothing moves yet). "
        "Provide transit-centred 'sources' [{label,ra_deg,dec_deg,window_min}] "
        "OR explicit 'segments' [{t_start,t_end,dec_deg,label}]. Then present "
        "the schedule and arm only after the user confirms.",
        lambda c, a: c.set_observing_plan(
            sources=a.get("sources"), segments=a.get("segments"),
            window_min=float(a.get("window_min", 30.0)), note=str(a.get("note", ""))),
        {"type": "object",
         "properties": {"sources": _ARR, "segments": _ARR,
                        "window_min": {"type": "number"},
                        "note": {"type": "string"}},
         "required": []}),
    ControlToolSpec(
        "observe_at_dec",
        "STAGE a single open-ended observation at a declination (executor only, "
        "unarmed). Leave end_unix empty for 'until further instructions'. Still "
        "needs confirmation + arm.",
        lambda c, a: c.observe_at_dec(
            float(a["dec_deg"]), label=str(a.get("label", "")),
            start_unix=a.get("start_unix"), end_unix=a.get("end_unix"),
            note=str(a.get("note", ""))),
        {"type": "object",
         "properties": {"dec_deg": {"type": "number"}, "label": {"type": "string"},
                        "start_unix": {"type": "number"},
                        "end_unix": {"type": "number"}, "note": {"type": "string"}},
         "required": ["dec_deg"]}),
    ControlToolSpec(
        "preview_observing_plan",
        "The exact bring-up steps (point/fstable/start-or-restart/warm/arm) the "
        "sequencer would run for each segment of the STAGED plan, from a current "
        "state snapshot. Show this to the user before arming.",
        lambda c, a: c.preview_observing_plan()),
    ControlToolSpec(
        "arm_observing_plan",
        "ARM the staged plan (executor only). Call this ONLY after presenting "
        "the full schedule and getting the user's explicit confirmation. Once "
        "armed the sequencer runs the bring-up autonomously; you do NOT confirm "
        "each command.",
        lambda c, a: c.arm_observing_plan()),
    ControlToolSpec(
        "disarm_observing_plan",
        "Disarm the plan so the sequencer stops acting on it (executor only).",
        lambda c, a: c.disarm_observing_plan()),
    ControlToolSpec(
        "observing_status",
        "Current plan: armed?, active segment, dec now.",
        lambda c, a: c.observing_status()),
    ControlToolSpec(
        "run_observing_step",
        "Manually advance the ARMED plan's bring-up by one step (executor "
        "only). The supervisor normally does this automatically.",
        lambda c, a: c.run_observing_step()),
    ControlToolSpec(
        "clear_plan", "Clear the active observing plan (executor only).",
        lambda c, a: c.clear_plan()),
]

CONTROL_SPECS_BY_NAME = {s.name: s for s in CONTROL_TOOL_SPECS}


__all__ = [
    "AgentControl", "ControlToolError", "ControlToolSpec",
    "CONTROL_TOOL_SPECS", "CONTROL_SPECS_BY_NAME",
]
