"""Autonomy supervisor (Phase 5).

The standing local brain-stem: a deterministic, non-LLM loop that ties the
monitoring + control machinery into the unprompted behaviours the operator
asked for —

* continuous **health monitoring** with alerting (always, when enabled),
* optional **auto-recovery** of known failures,
* periodic **injection health-checks** (end-to-end pulse tests),
* ticking the **observing-plan runner** on a cadence.

Safety model
------------
Monitoring is read-only and runs whenever the supervisor is ``enabled``.
The three *mutating* loops only act when ALL of the following hold:

* the loop's own flag is set in policy ``autonomy`` config,
* this session holds the executor **lease**,
* the dashboard has **not** locked agents out (``/cmd/operator/control``),
* the **e-stop** is not engaged.

Even then, every mutation is submitted through the full
:class:`ControlEngine` gauntlet, so a "recovery" during commissioning
surfaces as ``needs_approval`` and is logged rather than executed. The
supervisor never writes etcd or calls an executor directly.

The unit of work is :meth:`tick`, which is pure given an injected clock —
that's what the tests drive. :meth:`run` is a thin wrapper that calls
:meth:`tick` on a cadence until a stop event is set.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from dsa_operator.audit.log import AuditRecord
from dsa_operator.control.engine import ControlEngine, Outcome
from dsa_operator.monitor.health import (
    LEVEL_ALERT, HealthReport, HealthThresholds, evaluate_health)
from dsa_operator.monitor.injection import InjectionHealthCheck
from dsa_operator.monitor.recovery import RecoveryPlaybook

LOG = logging.getLogger("dsa_operator.monitor.supervisor")


@dataclass(frozen=True)
class AutonomyConfig:
    enabled: bool = False
    auto_recover: bool = False
    injection_health_check: bool = False
    run_plan: bool = False
    health_s: float = 60.0
    injection_s: float = 3600.0
    plan_s: float = 30.0
    verify_after_s: float = 180.0

    @classmethod
    def from_policy(cls, policy: Any) -> "AutonomyConfig":
        a = dict(getattr(policy, "autonomy", {}) or {})
        iv = dict(a.get("intervals", {}) or {})
        th = dict(a.get("thresholds", {}) or {})
        return cls(
            enabled=bool(a.get("enabled", False)),
            auto_recover=bool(a.get("auto_recover", False)),
            injection_health_check=bool(a.get("injection_health_check", False)),
            run_plan=bool(a.get("run_plan", False)),
            health_s=float(iv.get("health_s", 60.0)),
            injection_s=float(iv.get("injection_s", 3600.0)),
            plan_s=float(iv.get("plan_s", 30.0)),
            verify_after_s=float(th.get("injection_verify_after_s", 180.0)),
        )

    @property
    def min_interval_s(self) -> float:
        return max(1.0, min(self.health_s, self.injection_s, self.plan_s))


@dataclass
class SupervisorTick:
    ts: float
    ran: list[str] = field(default_factory=list)
    gated_out: bool = False
    gate_reason: str = ""
    health: Optional[HealthReport] = None
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    injection: Optional[dict[str, Any]] = None
    plan: Optional[dict[str, Any]] = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ts": self.ts, "ran": self.ran,
            "gated_out": self.gated_out, "gate_reason": self.gate_reason,
            "recoveries": self.recoveries, "notes": self.notes,
        }
        if self.health is not None:
            d["health"] = self.health.to_json()
        if self.injection is not None:
            d["injection"] = self.injection
        if self.plan is not None:
            d["plan"] = self.plan
        return d


class AutonomySupervisor:
    def __init__(
        self, engine: ControlEngine, tools: Any, audit: Any,
        config: AutonomyConfig, *,
        thresholds: Optional[HealthThresholds] = None,
        plan_runner: Any = None,
        injection: Optional[InjectionHealthCheck] = None,
        recovery: Optional[RecoveryPlaybook] = None,
        slack: Any = None,
        actor: str = "agent", session_id: str = "supervisor",
        now=time.time,
    ) -> None:
        self._engine = engine
        self._tools = tools
        self._audit = audit
        self.config = config
        self._thresholds = thresholds or HealthThresholds()
        self._plan = plan_runner
        self._injection = injection
        self._recovery = recovery or RecoveryPlaybook()
        self._slack = slack
        self.actor = actor
        self.session_id = session_id
        self._now = now
        self._last: dict[str, float] = {}       # loop -> last-run ts
        self._last_alert_codes: set[str] = set()
        # Edge-trigger the health audit row: remember the last (level, codes)
        # we wrote so a persistent condition (e.g. stale sky/SEFD while the
        # system is idle) isn't re-logged every tick.
        self._last_health_state: Optional[tuple[str, frozenset[str]]] = None
        self._last_tick: Optional[SupervisorTick] = None

    # -- gating ---------------------------------------------------------------
    def _mutation_gate(self) -> tuple[bool, str]:
        """May the supervisor submit mutating actions right now?"""
        try:
            if not self._engine.authority().agents_enabled:
                return False, "agents locked out from dashboard"
        except Exception:                                      # noqa: BLE001
            pass  # fail-open on authority read, like the engine itself
        try:
            if self._engine.is_paused():
                return False, "e-stop engaged"
        except Exception:                                      # noqa: BLE001
            pass
        try:
            if not self._engine.lease.held_by(self.session_id):
                return False, "this session does not hold the executor lease"
        except Exception:                                      # noqa: BLE001
            return False, "lease state unreadable"
        return True, ""

    def _due(self, loop: str, interval_s: float, t: float) -> bool:
        last = self._last.get(loop)
        return last is None or (t - last) >= interval_s

    # -- the tick -------------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> SupervisorTick:
        t = now if now is not None else self._now()
        tick = SupervisorTick(ts=t)
        if not self.config.enabled:
            tick.notes.append("supervisor disabled")
            self._last_tick = tick
            return tick

        # 1. health monitor (read-only; always runs when due) ---------------
        if self._due("health", self.config.health_s, t):
            self._last["health"] = t
            tick.ran.append("health")
            try:
                obs = self._engine.observation_status()
            except Exception:                                  # noqa: BLE001
                obs = None
            report = evaluate_health(self._tools, thresholds=self._thresholds,
                                     now=t, observation=obs)
            tick.health = report
            self._record_health(report)

        # 2. mutation gate (shared by the three acting loops) ---------------
        ok, reason = self._mutation_gate()
        tick.gated_out = not ok
        tick.gate_reason = reason

        # 3. auto-recovery --------------------------------------------------
        if ok and self.config.auto_recover and tick.health is not None:
            for prop in self._recovery.propose(tick.health):
                if not prop.auto:
                    tick.recoveries.append({**prop.to_json(), "submitted": False,
                                            "note": "manual (auto=false)"})
                    continue
                d = self._engine.evaluate(prop.action, prop.params,
                                          actor=self.actor, session_id=self.session_id)
                tick.recoveries.append({**prop.to_json(), "submitted": True,
                                        "outcome": d.outcome.value})
                tick.ran.append(f"recover:{prop.action}")

        # 4. injection health-check -----------------------------------------
        if ok and self.config.injection_health_check and self._injection is not None:
            inj_out: dict[str, Any] = {}
            if self._injection.due_to_verify(t):
                inj_out["verify"] = self._injection.verify(t).to_json()
                tick.ran.append("injection:verify")
            if not self._injection.in_flight and self._due("injection",
                                                            self.config.injection_s, t):
                self._last["injection"] = t
                d = self._injection.fire(t)
                inj_out["fire"] = {"outcome": d.outcome.value}
                tick.ran.append("injection:fire")
            if inj_out:
                tick.injection = inj_out

        # 5. observing-plan runner ------------------------------------------
        if ok and self.config.run_plan and self._plan is not None \
                and self._due("plan", self.config.plan_s, t):
            self._last["plan"] = t
            try:
                res = self._plan.apply(now=t)
                tick.plan = res.to_json()
                tick.ran.append("plan")
            except Exception as exc:                           # noqa: BLE001
                tick.notes.append(f"plan tick failed: {exc}")

        self._last_tick = tick
        return tick

    # -- health recording / alerting -----------------------------------------
    def _record_health(self, report: HealthReport) -> None:
        # Edge-triggered audit: only write a health_monitor row when the health
        # *state* changes — the overall level plus the set of non-OK finding
        # codes. Otherwise an idle system (persistently stale sky/SEFD ->
        # level=warn) would log an identical row every tick forever. State
        # transitions (degrade, clear, a new finding) are still recorded, so
        # the audit trail keeps the edges without the noise.
        state = (report.level, frozenset(report.codes))
        if state != self._last_health_state:
            self._last_health_state = state
            try:
                self._audit.record(AuditRecord(
                    action="health_monitor", kind="system", actor=self.actor,
                    ok=(report.level != LEVEL_ALERT), note=f"level={report.level}",
                    result=report.to_json()))
            except Exception:                                  # noqa: BLE001
                pass
        # Alert on newly-appearing alert codes only (edge-triggered) so we
        # don't spam Slack every tick while a condition persists.
        alert_codes = {f.code for f in report.alerts}
        new = alert_codes - self._last_alert_codes
        self._last_alert_codes = alert_codes
        if new and self._slack is not None:
            try:
                lines = [f"• {f.message}" for f in report.alerts if f.code in new]
                self._slack.post("🔴 dsa110-operator health alert:\n" + "\n".join(lines))
            except Exception:                                  # noqa: BLE001
                LOG.exception("slack alert post failed")

    # -- status / run loop ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "loops": {
                "auto_recover": self.config.auto_recover,
                "injection_health_check": self.config.injection_health_check,
                "run_plan": self.config.run_plan,
            },
            "intervals": {"health_s": self.config.health_s,
                          "injection_s": self.config.injection_s,
                          "plan_s": self.config.plan_s},
            "last_run": dict(self._last),
            "active_alerts": sorted(self._last_alert_codes),
            "last_tick": self._last_tick.to_json() if self._last_tick else None,
        }

    def run(self, stop_event: threading.Event) -> None:
        """Call :meth:`tick` on a cadence until ``stop_event`` is set."""
        LOG.info("autonomy supervisor up (enabled=%s)", self.config.enabled)
        interval = self.config.min_interval_s
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:                                  # noqa: BLE001
                LOG.exception("supervisor tick failed (continuing)")
            stop_event.wait(interval)
        LOG.info("autonomy supervisor stopped")


def maintain_lease(lease: Any, actor: str, session_id: str) -> str:
    """One heartbeat of a *standing* executor's lease ownership.

    The standing supervisor should hold the executor lease whenever it is
    free, but **yield** to a human who takes over and **reclaim** the moment
    that human releases it (or their session lapses). ``acquire()`` is a no-op
    while another session holds the lease, so calling it whenever we don't hold
    it gives exactly that arbitration — there is never a fight, and recovery is
    automatic.

    Returns one of:

    * ``"held"``         — we already hold it; just refreshed.
    * ``"acquired"``     — it was free and we (re)claimed it (startup, or after
      the operator released control / their session lapsed).
    * ``"monitor_only"`` — someone else holds it; we stay read-only for now.
    """
    state = lease.keepalive()
    if state == "held":
        return "held"
    # "idle" (we hold nothing) or "lost" (taken over / lapsed): reclaim iff free.
    return "acquired" if lease.acquire(actor, session_id) else "monitor_only"


def main() -> int:  # pragma: no cover
    """Standing-executor entrypoint: ``python -m dsa_operator.monitor.supervisor``.

    Wires the real engine + tools over the SSH-forwarded etcd/dashboard and
    runs the tick loop until SIGINT/SIGTERM. It tries to hold the executor
    lease as session ``"supervisor"`` (so the mutating loops are eligible), but
    starts in monitor-only mode if a human already holds control and
    **automatically takes over the moment they release it** — so a schedule
    armed from a laptop keeps running here after the laptop releases the lease
    (or sleeps). No policy change is needed for that: ``autonomy.run_plan`` is
    on by default; real (vs shadow) execution still needs ``mode: live`` +
    promotion, exactly as in the console.
    """
    import argparse
    import os
    import signal

    from dsa_operator.audit.egress import maybe_install_from_env
    from dsa_operator.env import load_secrets
    from dsa_operator.observing.plan import PlanStore
    from dsa_operator.observing.session import ObservingSequencer, ToolsSiteState
    from dsa_operator.web.app import (
        _default_audit, _default_control_engine, _default_tools_factory)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    load_secrets()
    maybe_install_from_env()
    ap = argparse.ArgumentParser(description="dsa110-operator autonomy supervisor")
    ap.add_argument("--actor", default=os.environ.get("DSA_OPERATOR_ACTOR", "agent"))
    args = ap.parse_args()

    audit = _default_audit()
    engine = _default_control_engine(audit)
    tools = _default_tools_factory(audit)("agent")
    sid = "supervisor"

    if maintain_lease(engine.lease, args.actor, sid) == "acquired":
        LOG.info("acquired executor lease as %s/%s", args.actor, sid)
    else:
        h = engine.lease.holder()
        LOG.warning("executor lease currently held by %s — starting MONITOR-ONLY; "
                    "will take over automatically when it is released",
                    h.actor if h else "?")

    plan_store = PlanStore(engine._writer, engine._read)  # type: ignore[attr-defined]
    # The sequencer runs the full bring-up (point -> fstable -> start/restart
    # -> warm -> arm) for each segment of an *armed* plan; staged-only plans
    # are ignored until a human confirms and arms them.
    runner = ObservingSequencer(engine, plan_store, ToolsSiteState(tools),
                                actor=args.actor, session_id=sid)
    cfg = AutonomyConfig.from_policy(engine.policy)
    # Edge-triggered health alerts -> Slack (if a webhook is configured).
    slack = None
    if os.environ.get("DSA_OPERATOR_SLACK_WEBHOOK_URL") \
            or os.environ.get("DSA_OPERATOR_SLACK_WEBHOOK"):
        from dsa_operator.audit.slack import SlackNotifier
        slack = SlackNotifier()
    sup = AutonomySupervisor(
        engine, tools, audit, cfg, plan_runner=runner, slack=slack,
        injection=InjectionHealthCheck(engine, tools, audit, actor=args.actor,
                                       session_id=sid, verify_after_s=cfg.verify_after_s),
        actor=args.actor, session_id=sid)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    # Keep the lease warm alongside the tick loop, and (re)claim it whenever it
    # is free: if this host sleeps the lease lapses and we retake it on wake; if
    # an operator takes over from a console we yield, then automatically take
    # back over when they release (or their session lapses) so an armed plan
    # keeps running here. acquire() only succeeds when the lease is free, so
    # this never fights a live operator.
    def _refresh():
        prev = "acquired"
        while not stop.wait(min(10.0, cfg.min_interval_s)):
            try:
                state = maintain_lease(engine.lease, args.actor, sid)
                if state == "acquired" and prev == "monitor_only":
                    LOG.warning("reclaimed executor lease — standing executor is "
                                "driving again (operator released control)")
                elif state == "monitor_only" and prev != "monitor_only":
                    h = engine.lease.holder()
                    LOG.info("executor lease held by %s — standing executor is "
                             "monitor-only until it is released",
                             h.actor if h else "?")
                prev = state
            except Exception:                                  # noqa: BLE001
                LOG.exception("lease keepalive failed")
    threading.Thread(target=_refresh, daemon=True).start()
    try:
        sup.run(stop)
    finally:
        engine.lease.release(sid)
    return 0


__all__ = ["AutonomyConfig", "SupervisorTick", "AutonomySupervisor",
           "maintain_lease", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
