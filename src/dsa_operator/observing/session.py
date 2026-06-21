"""Observation bring-up sequencer.

Turns "observe at declination D" into the exact, ordered bring-up the
operator runs by hand, with each mutating step funnelled through the
:class:`~dsa_operator.control.engine.ControlEngine` (so it obeys the lease,
e-stop, dashboard lockout, gates, and shadow/live mode):

1. **point** the array to D (if it isn't already there), then wait for the
   dishes to settle;
2. ensure a **fringe-stopping table** for D exists (build + deploy if not);
3. **start** the fleet, or **restart_all** it if it's already running (so it
   re-reads the new dec / fstable);
4. wait until the fleet reports **warmed / safe to arm** (the dashboard's own
   ``system_state`` — ``preparing`` ⇒ warming, ``prepared`` ⇒ safe);
5. **arm** recording with ``utc_start`` (default holdoff/margin 60000).

The sequencer is a deterministic state machine advanced by :meth:`step`
(one action per call) so it composes with a tick loop. In **shadow** mode it
walks the whole sequence without gating on real state (a dry run / preview);
in **live** mode it gates each wait on the real readings. :meth:`describe`
renders the intended steps for *plan-level confirmation* before anything
runs.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Protocol

from dsa_operator.control.engine import ControlEngine, Decision, Outcome
from dsa_operator.observing import astro

LOG = logging.getLogger("dsa_operator.observing.session")

DEFAULT_HOLDOFF = 60000


class Stage(str, Enum):
    INIT = "init"
    POINT = "point"
    SETTLE = "settle"
    FSTABLE = "fstable"
    MODES = "modes"
    FLEET = "fleet"
    WARM = "warm"
    ARM = "arm"
    DONE = "done"
    BLOCKED = "blocked"


# -- per-DEC mode setup (extensible) ----------------------------------------

@dataclass(frozen=True)
class ModeApplier:
    """Maps a per-segment ``setup`` key to a control action applied during
    bring-up. ``build_params`` turns the segment's config for that key into the
    action's params. Appliers run in the ``MODES`` stage — *before* the fleet
    starts — because mode changes (e.g. spectral line) take effect at the next
    fleet start. Register new modes with :func:`register_mode_applier`."""

    key: str
    action: str
    build_params: Callable[[dict[str, Any]], dict[str, Any]]
    describe: Callable[[dict[str, Any]], str] = lambda c: ""


def _spectral_line_params(cfg: dict[str, Any]) -> dict[str, Any]:
    if "subbands" not in cfg:
        raise ValueError("spectral_line setup needs 'subbands'")
    return {"subbands": cfg["subbands"],
            "reason": str(cfg.get("reason", "per-dec observing plan"))}


def _spectral_line_describe(cfg: dict[str, Any]) -> str:
    sb = cfg.get("subbands")
    return f"set_spectral_line subbands={sb!r}" if sb else "continuum (no spectral line)"


#: name -> applier. New per-DEC modes (beyond spectral line) register here, so
#: the bring-up sequence is extensible without touching the state machine.
MODE_APPLIERS: dict[str, ModeApplier] = {
    "spectral_line": ModeApplier(
        "spectral_line", "set_spectral_line", _spectral_line_params,
        _spectral_line_describe),
}


def register_mode_applier(applier: ModeApplier) -> None:
    MODE_APPLIERS[applier.key] = applier


# -- the read-only site view the sequencer needs ----------------------------

class SiteState(Protocol):
    def commanded_dec(self) -> Optional[float]: ...
    def n_not_settled(self) -> Optional[int]: ...
    def fleet_state(self) -> dict[str, Any]: ...        # {state, safe_to_arm}
    def fstable_status(self, dec_deg: float) -> dict[str, Any]: ...


def _data(d: Any) -> dict[str, Any]:
    """Normalize a tool return into the payload dict.

    ``ReadOnlyTools`` methods return the summary dict *directly* (e.g.
    ``{"target_dec_deg": ...}``), while some callers/transports wrap it as
    ``{"ok": True, "data": {...}}``. Accept both: prefer an explicit ``data``
    envelope, else use the dict as-is. (Previously this always unwrapped
    ``data`` and so returned ``{}`` for the real ReadOnlyTools, leaving the
    sequencer blind to commanded dec / settle / fleet state / fstable.)"""
    if not isinstance(d, dict):
        return {}
    inner = d.get("data")
    return inner if isinstance(inner, dict) else d


class ToolsSiteState:
    """Concrete :class:`SiteState` over the read-only tools."""

    def __init__(self, tools: Any) -> None:
        self._tools = tools

    def commanded_dec(self) -> Optional[float]:
        try:
            v = _data(self._tools.get_array_pointing()).get("target_dec_deg")
            return float(v) if v is not None else None
        except Exception:                                  # noqa: BLE001
            return None

    def n_not_settled(self) -> Optional[int]:
        try:
            v = _data(self._tools.get_array_pointing()).get("n_not_settled")
            return int(v) if v is not None else None
        except Exception:                                  # noqa: BLE001
            return None

    def fleet_state(self) -> dict[str, Any]:
        try:
            ss = _data(self._tools.get_fleet_status()).get("system_state", {})
            return ss if isinstance(ss, dict) else {}
        except Exception:                                  # noqa: BLE001
            return {}

    def fstable_status(self, dec_deg: float) -> dict[str, Any]:
        try:
            return _data(self._tools.get_fstable_status(dec_deg))
        except Exception as exc:                           # noqa: BLE001
            return {"error": str(exc)}


def _fstable_ready(status: dict[str, Any]) -> Optional[bool]:
    """True if every corr node already has the table for the dec."""
    if not isinstance(status, dict) or status.get("error"):
        return None
    if "all_ready" in status:
        return bool(status["all_ready"])
    light = status.get("light")
    if light is not None:
        return light == "green"
    return None


@dataclass
class StepResult:
    stage: str
    action: Optional[str] = None
    detail: str = ""
    decision: Optional[Decision] = None
    waiting: bool = False
    done: bool = False
    blocked: bool = False

    def to_json(self) -> dict[str, Any]:
        d = {"stage": self.stage, "action": self.action, "detail": self.detail,
             "waiting": self.waiting, "done": self.done, "blocked": self.blocked}
        if self.decision is not None:
            d["decision"] = self.decision.to_json()
        return d


@dataclass
class BringUp:
    """State machine that brings observing up at one declination."""

    engine: ControlEngine
    site: SiteState
    dec_deg: float
    actor: str
    session_id: str
    holdoff: int = DEFAULT_HOLDOFF
    dec_tol_deg: float = 0.25
    ensure_fstable: bool = True
    require_settle: bool = True
    require_warm: bool = True
    restart_if_running: bool = True
    setup: dict[str, Any] = field(default_factory=dict)
    settle_timeout_s: float = 240.0
    fstable_timeout_s: float = 1800.0
    warm_timeout_s: float = 600.0
    now: Any = time.time

    stage: Stage = field(default=Stage.INIT, init=False)
    reason: str = field(default="", init=False)
    _pointed: bool = field(default=False, init=False)
    _fstable_built: bool = field(default=False, init=False)
    _modes_done: set = field(default_factory=set, init=False)
    _deadline: Optional[float] = field(default=None, init=False)

    # -- helpers --------------------------------------------------------------
    @property
    def _shadow(self) -> bool:
        return self.engine.policy.mode != "live"

    @property
    def done(self) -> bool:
        return self.stage in (Stage.DONE, Stage.BLOCKED)

    def _el(self) -> float:
        lat = float(self.engine.policy.pointing.get("lat_ovro_deg", astro.OVRO_LAT_DEG))
        return astro.dec_to_el(self.dec_deg, lat)

    def _need_point(self) -> bool:
        cur = self.site.commanded_dec()
        return cur is None or abs(cur - self.dec_deg) > self.dec_tol_deg

    def _evaluate(self, action: str, params: dict[str, Any]) -> Decision:
        return self.engine.evaluate(action, params, actor=self.actor,
                                    session_id=self.session_id)

    def _ok(self, decision: Decision) -> bool:
        return decision.outcome in (Outcome.EXECUTED, Outcome.SHADOW)

    def _arm_deadline(self, secs: float) -> None:
        self._deadline = self.now() + secs

    def _expired(self) -> bool:
        return self._deadline is not None and self.now() > self._deadline

    # -- preview --------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """The intended bring-up for this dec, from a current state snapshot.
        Read-only; safe to call any time (used for confirmation)."""
        steps: list[str] = []
        el = self._el()
        if self._need_point():
            cur = self.site.commanded_dec()
            steps.append(f"point_array → dec {self.dec_deg:.4f} (el {el:.3f}); "
                         f"currently {('dec %.4f' % cur) if cur is not None else 'unknown'}")
            will_point = True
        else:
            steps.append(f"already at dec {self.dec_deg:.4f} (el {el:.3f}); no slew")
            will_point = False
        if self.ensure_fstable:
            ready = _fstable_ready(self.site.fstable_status(self.dec_deg))
            if ready:
                steps.append("fringe-stop table present on all corr nodes")
            else:
                steps.append("build_fstable + deploy_fstable (table missing)")
        for key, cfg in (self.setup or {}).items():
            applier = MODE_APPLIERS.get(key)
            if applier is None:
                steps.append(f"⚠ unknown mode '{key}' (no applier registered)")
            else:
                cfgd = dict(cfg) if isinstance(cfg, dict) else {}
                steps.append(applier.describe(cfgd) or f"apply mode '{key}'")
        st = self.site.fleet_state()
        state = (st.get("state") or "unknown")
        if state in ("offline", "unknown", None):
            steps.append(f"start_fleet (currently {state})")
        elif will_point and self.restart_if_running:
            steps.append(f"restart_all (running as {state}; re-read new dec/fstable)")
        else:
            steps.append(f"fleet already running ({state}); no restart")
        steps.append("wait until warmed (system_state → prepared / safe_to_arm)")
        steps.append(f"utc_start (arm, margin/holdoff = {self.holdoff})")
        return {"dec_deg": self.dec_deg, "el_deg": round(el, 3),
                "holdoff": self.holdoff, "mode": self.engine.policy.mode,
                "setup": dict(self.setup or {}), "steps": steps}

    # -- the state machine ----------------------------------------------------
    def step(self) -> StepResult:
        m = getattr(self, f"_stage_{self.stage.value}", None)
        if m is None:
            return self._block(f"no handler for stage {self.stage}")
        return m()

    def run(self, max_steps: int = 12) -> StepResult:
        """Advance until done/blocked or a wait that can't progress now."""
        res = StepResult(self.stage.value, detail="noop")
        for _ in range(max_steps):
            res = self.step()
            if res.done or res.blocked or res.waiting:
                break
        return res

    def _block(self, reason: str) -> StepResult:
        self.stage = Stage.BLOCKED
        self.reason = reason
        return StepResult(self.stage.value, detail=reason, blocked=True)

    def _advance(self, stage: Stage, detail: str) -> StepResult:
        self.stage = stage
        self._deadline = None
        return StepResult(self.stage.value, detail=detail)

    def _act(self, action: str, params: dict[str, Any], nxt: Stage,
             detail: str) -> StepResult:
        d = self._evaluate(action, params)
        if not self._ok(d):
            return self._block(f"{action} → {d.outcome.value}: {d.reason}")
        self.stage = nxt
        self._deadline = None
        return StepResult(nxt.value, action=action, detail=detail, decision=d)

    # stage handlers
    def _stage_init(self) -> StepResult:
        if self._need_point():
            return self._advance(Stage.POINT, "slew required")
        self._pointed = False
        return self._advance(_fstable_or_fleet(self), "on target")

    def _stage_point(self) -> StepResult:
        self._pointed = True
        return self._act("point_array", {"dec_deg": self.dec_deg},
                         Stage.SETTLE, f"slew to dec {self.dec_deg:.4f}")

    def _stage_settle(self) -> StepResult:
        if self._shadow or not self.require_settle:
            return self._advance(_next_after_settle(self), "settle skipped (shadow)")
        n = self.site.n_not_settled()
        if n == 0:
            return self._advance(_next_after_settle(self), "dishes settled")
        if self._deadline is None:
            self._arm_deadline(self.settle_timeout_s)
        if self._expired():
            return self._block("antennas did not settle in time")
        return StepResult(self.stage.value, detail=f"settling ({n} not settled)",
                          waiting=True)

    def _stage_fstable(self) -> StepResult:
        ready = _fstable_ready(self.site.fstable_status(self.dec_deg))
        if ready:
            return self._advance(Stage.MODES, "fstable present")
        if self._shadow:
            d = self._evaluate("build_fstable", {"dec_deg": self.dec_deg})
            if not self._ok(d):
                return self._block(f"build_fstable → {d.outcome.value}")
            self.stage = Stage.MODES
            self._deadline = None
            return StepResult(Stage.MODES.value, action="build_fstable",
                              detail="fstable build+deploy (shadow)", decision=d)
        # live: build, wait for presence, then deploy
        if not self._fstable_built:
            d = self._evaluate("build_fstable", {"dec_deg": self.dec_deg})
            if not self._ok(d):
                return self._block(f"build_fstable → {d.outcome.value}: {d.reason}")
            self._fstable_built = True
            self._arm_deadline(self.fstable_timeout_s)
            return StepResult(self.stage.value, action="build_fstable",
                              detail="building fstable", decision=d, waiting=True)
        status = self.site.fstable_status(self.dec_deg)
        fn = status.get("filename") or status.get("expected_filename")
        if fn and not status.get("master_present", True) is False:
            d = self._evaluate("deploy_fstable", {"filename": fn})
            if self._ok(d):
                return StepResult(self.stage.value, action="deploy_fstable",
                                  detail=f"deploying {fn}", decision=d, waiting=True)
        if self._expired():
            return self._block("fstable not ready in time (build/deploy by hand)")
        return StepResult(self.stage.value, detail="waiting for fstable",
                          waiting=True)

    def _stage_modes(self) -> StepResult:
        # Apply each registered per-DEC mode (e.g. spectral line) BEFORE the
        # fleet starts, since those take effect at the next start. Extensible:
        # any key in segment.setup with a registered applier runs here.
        for key, cfg in (self.setup or {}).items():
            if key in self._modes_done:
                continue
            applier = MODE_APPLIERS.get(key)
            if applier is None:
                return self._block(f"no mode applier registered for setup '{key}'")
            try:
                params = applier.build_params(dict(cfg) if isinstance(cfg, dict) else {})
            except (ValueError, KeyError, TypeError) as exc:
                return self._block(f"bad setup for '{key}': {exc}")
            d = self._evaluate(applier.action, params)
            if not self._ok(d):
                return self._block(f"{applier.action} → {d.outcome.value}: {d.reason}")
            self._modes_done.add(key)
            return StepResult(self.stage.value, action=applier.action,
                              detail=f"mode '{key}' applied", decision=d)
        return self._advance(Stage.FLEET, "modes set")

    def _stage_fleet(self) -> StepResult:
        st = self.site.fleet_state()
        state = st.get("state") or "unknown"
        if state in ("offline", "unknown", None):
            return self._act("start_fleet", {"dec_deg": self.dec_deg},
                             Stage.WARM, "start fleet")
        if self._pointed and self.restart_if_running:
            return self._act("restart_all", {"dec_deg": self.dec_deg},
                             Stage.WARM, "restart fleet (new dec)")
        return self._advance(Stage.WARM, f"fleet already running ({state})")

    def _stage_warm(self) -> StepResult:
        if self._shadow or not self.require_warm:
            return self._advance(Stage.ARM, "warm skipped (shadow)")
        st = self.site.fleet_state()
        state = st.get("state")
        if state == "observing":
            return self._advance(Stage.DONE, "already observing")
        if st.get("safe_to_arm") or state == "prepared":
            return self._advance(Stage.ARM, "warmed; safe to arm")
        if self._deadline is None:
            self._arm_deadline(self.warm_timeout_s)
        if self._expired():
            return self._block("fleet did not warm in time")
        return StepResult(self.stage.value, detail=f"warming ({state})",
                          waiting=True)

    def _stage_arm(self) -> StepResult:
        res = self._act("utc_start", {"margin": int(self.holdoff)},
                        Stage.DONE, f"arm (margin={self.holdoff})")
        return res

    def _stage_done(self) -> StepResult:
        return StepResult(Stage.DONE.value, detail="observing", done=True)

    def _stage_blocked(self) -> StepResult:
        return StepResult(Stage.BLOCKED.value, detail=self.reason, blocked=True)


def _fstable_or_fleet(bu: "BringUp") -> Stage:
    return Stage.FSTABLE if bu.ensure_fstable else Stage.MODES


_next_after_settle = _fstable_or_fleet


# -- the per-segment driver -------------------------------------------------

@dataclass
class SeqResult:
    active: bool
    reason: str
    segment_label: str = ""
    target_dec: Optional[float] = None
    stage: str = ""
    step: Optional[StepResult] = None

    def to_json(self) -> dict[str, Any]:
        d = {"active": self.active, "reason": self.reason,
             "segment_label": self.segment_label, "target_dec": self.target_dec,
             "stage": self.stage}
        if self.step is not None:
            d["step"] = self.step.to_json()
        return d


class ObservingSequencer:
    """Drives the bring-up for whichever segment of an *armed* plan is active.

    Drop-in for the supervisor's plan loop: :meth:`apply` advances the current
    segment's :class:`BringUp` and returns a JSON-able result. A plan that is
    only *staged* (``armed == False``) is ignored — nothing moves until a
    human has confirmed the schedule and it has been armed.
    """

    def __init__(
        self, engine: ControlEngine, plan_store: Any, site: SiteState, *,
        actor: str, session_id: str, holdoff: int = DEFAULT_HOLDOFF,
        dec_tol_deg: float = 0.25, now: Any = time.time,
        bringup_kwargs: Optional[dict[str, Any]] = None,
    ) -> None:
        self._engine = engine
        self._plans = plan_store
        self._site = site
        self.actor = actor
        self.session_id = session_id
        self.holdoff = int(holdoff)
        self.dec_tol = float(dec_tol_deg)
        self._now = now
        self._kw = dict(bringup_kwargs or {})
        self._key: Optional[tuple] = None
        self._bu: Optional[BringUp] = None

    def _reset(self) -> None:
        self._key = None
        self._bu = None

    def _new_bringup(self, dec: float, setup: Optional[dict] = None) -> BringUp:
        return BringUp(self._engine, self._site, dec_deg=dec, actor=self.actor,
                       session_id=self.session_id, holdoff=self.holdoff,
                       dec_tol_deg=self.dec_tol, setup=dict(setup or {}),
                       now=self._now, **self._kw)

    def describe_plan(self, now: Optional[float] = None) -> dict[str, Any]:
        """Per-segment bring-up preview for the staged plan (confirmation)."""
        plan = self._plans.get()
        if plan is None:
            return {"plan": None}
        segs = []
        for s in plan.segments:
            bu = self._new_bringup(s.dec_deg, s.setup)
            segs.append({"label": s.label, "t_start": s.t_start,
                         "t_end": s.t_end, **bu.describe()})
        return {"armed": plan.armed, "n_segments": len(plan.segments),
                "segments": segs}

    def status(self) -> dict[str, Any]:
        bu = self._bu
        return {"stage": bu.stage.value if bu else "idle",
                "dec_deg": bu.dec_deg if bu else None,
                "reason": (bu.reason if bu else "")}

    def apply(self, now: Optional[float] = None) -> SeqResult:
        t = now if now is not None else self._now()
        plan = self._plans.get()
        if plan is None:
            self._reset()
            return SeqResult(False, "no active plan")
        if not plan.armed:
            self._reset()
            return SeqResult(False, "plan staged but not armed")
        seg = plan.active_at(t)
        if seg is None:
            self._reset()
            nxt = plan.next_segment(t)
            reason = "no active segment now"
            if nxt is not None:
                reason += f"; next {nxt.label or 'segment'} in {nxt.t_start - t:.0f}s"
            return SeqResult(False, reason)
        key = (round(seg.t_start, 3), round(seg.dec_deg, 4))
        if key != self._key or self._bu is None:
            self._key = key
            self._bu = self._new_bringup(seg.dec_deg, seg.setup)
        if self._bu.done:
            return SeqResult(True, f"bring-up {self._bu.stage.value}",
                             seg.label, seg.dec_deg, self._bu.stage.value)
        step = self._bu.run()
        return SeqResult(True, step.detail, seg.label, seg.dec_deg,
                         self._bu.stage.value, step)


__all__ = ["Stage", "SiteState", "ToolsSiteState", "BringUp", "StepResult",
           "SeqResult", "ObservingSequencer", "DEFAULT_HOLDOFF",
           "ModeApplier", "MODE_APPLIERS", "register_mode_applier"]
