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
from dsa_operator.observing.plan import ObservingPlan, PlanError, PlanStore
from dsa_operator.observing.runner import PlanRunner

LOG = logging.getLogger("dsa_operator.agent.control")


class ControlToolError(RuntimeError):
    pass


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
        actor: str, session_id: str, now=time.time,
    ) -> None:
        self._engine = engine
        self._plans = plan_store
        self._read = read_etcd
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
        self._plans.set(plan)
        self._audit("set_observing_plan", ok=True,
                    params={"n_segments": len(plan.segments)})
        return {"ok": True, "n_segments": len(plan.segments),
                "plan": plan.to_json()}

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
        "set_observing_plan",
        "Install an observing plan (executor only). Provide transit-centred "
        "'sources' [{label,ra_deg,dec_deg,window_min}] OR explicit 'segments' "
        "[{t_start,t_end,dec_deg,label}].",
        lambda c, a: c.set_observing_plan(
            sources=a.get("sources"), segments=a.get("segments"),
            window_min=float(a.get("window_min", 30.0)), note=str(a.get("note", ""))),
        {"type": "object",
         "properties": {"sources": _ARR, "segments": _ARR,
                        "window_min": {"type": "number"},
                        "note": {"type": "string"}},
         "required": []}),
    ControlToolSpec(
        "preview_plan",
        "What the plan runner WOULD do now (no engine call, no move).",
        lambda c, a: c.preview_plan()),
    ControlToolSpec(
        "tick_plan",
        "Run one plan step now: issue point_array through the engine if the "
        "active dec differs from the commanded dec (executor only).",
        lambda c, a: c.tick_plan()),
    ControlToolSpec(
        "clear_plan", "Clear the active observing plan (executor only).",
        lambda c, a: c.clear_plan()),
]

CONTROL_SPECS_BY_NAME = {s.name: s for s in CONTROL_TOOL_SPECS}


__all__ = [
    "AgentControl", "ControlToolError", "ControlToolSpec",
    "CONTROL_TOOL_SPECS", "CONTROL_SPECS_BY_NAME",
]
