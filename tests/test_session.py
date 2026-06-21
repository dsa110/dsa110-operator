"""Bring-up sequencer: state machine, per-DEC modes, arming, transits."""
from __future__ import annotations

import dataclasses

import pytest

from dsa_operator.agent.control import AgentControl, ControlToolError
from dsa_operator.audit.log import AuditLog
from dsa_operator.control.approvals import ApprovalStore
from dsa_operator.control.engine import ControlEngine
from dsa_operator.control.lease import ExecutorLease
from dsa_operator.etcd.write import FakeOperatorBackend, OperatorEtcdWriter
from dsa_operator.observing.plan import ObservingPlan, PlanStore, Segment
from dsa_operator.observing.session import (
    BringUp, ObservingSequencer, Stage)
from dsa_operator.policy import load_policy

SID = "sid-alice"
ACTOR = "alice@dsa110.org"


class _SharedStore:
    def __init__(self):
        self._d = {}

    def put(self, key, value, lease_id=None):
        self._d[key] = value

    def delete(self, key):
        self._d.pop(key, None)

    def get_dict(self, key):
        return self._d.get(key)


class FakeSite:
    def __init__(self, *, dec=20.0, not_settled=0, state="offline",
                 fstable_ready=False):
        self.dec = dec
        self.not_settled = not_settled
        self.state = state
        self._fstable_ready = fstable_ready

    def commanded_dec(self):
        return self.dec

    def n_not_settled(self):
        return self.not_settled

    def fleet_state(self):
        return {"state": self.state,
                "safe_to_arm": self.state in ("prepared", "observing")}

    def fstable_status(self, dec_deg):
        return {"all_ready": self._fstable_ready}


@pytest.fixture()
def engine(tmp_path):
    # Pin shadow so these state-machine tests are independent of whatever
    # mode the operator has the live config/policy.yaml set to.
    shared = _SharedStore()
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    audit = AuditLog(tmp_path / "audit")
    policy = dataclasses.replace(load_policy(), mode="shadow")
    eng = ControlEngine(policy, ExecutorLease(writer), ApprovalStore(),
                        audit, writer=writer)
    eng.lease.acquire(ACTOR, SID)
    eng._shared = shared            # stash for tests that want a plan store
    return eng


def _live_engine(tmp_path, *, observing=None):
    """An engine in LIVE mode (so the sequencer gates on real state), with an
    optional `observing:` policy block for the dec-ready arm override."""
    shared = _SharedStore()
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    audit = AuditLog(tmp_path / "audit")
    policy = dataclasses.replace(load_policy(), mode="live",
                                 observing=dict(observing or {}))
    eng = ControlEngine(policy, ExecutorLease(writer), ApprovalStore(),
                        audit, writer=writer)
    eng.lease.acquire(ACTOR, SID)
    eng._shared = shared
    return eng


def _actions(bu: BringUp) -> list[str]:
    """Walk the machine, collecting the control actions it issues."""
    seen: list[str] = []
    for _ in range(40):
        res = bu.step()
        if res.action:
            seen.append(res.action)
        if res.done or res.blocked:
            break
    return seen


def test_shadow_full_bringup_from_cold(engine):
    bu = BringUp(engine, FakeSite(dec=20.0, state="offline"),
                 dec_deg=69.04, actor=ACTOR, session_id=SID)
    acts = _actions(bu)
    assert bu.stage is Stage.DONE
    assert acts == ["point_array", "build_fstable", "start_fleet", "utc_start"]


def test_already_on_target_with_fstable_and_running_restarts(engine):
    # On target, fstable present, fleet already running -> no point/build, and
    # because we did NOT slew, no restart either; just arm.
    bu = BringUp(engine, FakeSite(dec=69.04, state="prepared",
                                  fstable_ready=True),
                 dec_deg=69.04, actor=ACTOR, session_id=SID)
    acts = _actions(bu)
    assert bu.stage is Stage.DONE
    assert acts == ["utc_start"]


def test_slew_while_running_triggers_restart_all(engine):
    bu = BringUp(engine, FakeSite(dec=10.0, state="prepared",
                                  fstable_ready=True),
                 dec_deg=69.04, actor=ACTOR, session_id=SID)
    acts = _actions(bu)
    assert "point_array" in acts and "restart_all" in acts
    assert "start_fleet" not in acts
    assert acts[-1] == "utc_start"


def test_per_dec_spectral_line_mode_applies_before_fleet(engine):
    bu = BringUp(engine, FakeSite(dec=69.04, state="offline",
                                  fstable_ready=True),
                 dec_deg=69.04, actor=ACTOR, session_id=SID,
                 setup={"spectral_line": {"subbands": [3, 4]}})
    acts = _actions(bu)
    assert bu.stage is Stage.DONE
    # spectral line is set before the fleet starts
    assert acts.index("set_spectral_line") < acts.index("start_fleet")


def test_unknown_mode_blocks(engine):
    bu = BringUp(engine, FakeSite(fstable_ready=True),
                 dec_deg=33.0, actor=ACTOR, session_id=SID,
                 setup={"no_such_mode": {}})
    _actions(bu)
    assert bu.stage is Stage.BLOCKED
    assert "no_such_mode" in bu.reason


def test_describe_lists_steps_and_modes(engine):
    bu = BringUp(engine, FakeSite(dec=20.0, state="offline"),
                 dec_deg=69.04, actor=ACTOR, session_id=SID,
                 holdoff=60000, setup={"spectral_line": {"subbands": [1]}})
    d = bu.describe()
    joined = " | ".join(d["steps"])
    assert "point_array" in joined
    assert "build_fstable" in joined
    assert "set_spectral_line" in joined
    assert "60000" in joined
    assert d["setup"] == {"spectral_line": {"subbands": [1]}}


# -- dec-ready arm override (live) ------------------------------------------

def test_warm_waits_without_override(tmp_path):
    # Live, on target, fstable present, fleet "ready" (NOT prepared) and
    # safe_to_arm false: with no override the bring-up stalls at WARM.
    eng = _live_engine(tmp_path)
    bu = BringUp(eng, FakeSite(dec=69.04, state="ready", fstable_ready=True,
                               not_settled=2),
                 dec_deg=69.04, actor=ACTOR, session_id=SID)
    _actions(bu)
    assert bu.stage is Stage.WARM


def test_warm_dec_ready_override_arms_when_not_safe(tmp_path):
    # Same situation, but the override is enabled and only 2 dishes are moving
    # (≤ 4): the sequencer arms despite safe_to_arm being false.
    eng = _live_engine(tmp_path, observing={"arm_on_dec_ready": True,
                                            "max_moving_antennas": 4})
    bu = BringUp(eng, FakeSite(dec=69.04, state="ready", fstable_ready=True,
                               not_settled=2),
                 dec_deg=69.04, actor=ACTOR, session_id=SID)
    acts = _actions(bu)
    assert bu.stage is Stage.DONE
    assert acts == ["utc_start"]


def test_warm_override_respects_max_moving(tmp_path):
    # Too many dishes moving (5 > 4): the override does NOT fire; still waits.
    eng = _live_engine(tmp_path, observing={"arm_on_dec_ready": True,
                                            "max_moving_antennas": 4})
    bu = BringUp(eng, FakeSite(dec=69.04, state="ready", fstable_ready=True,
                               not_settled=5),
                 dec_deg=69.04, actor=ACTOR, session_id=SID)
    _actions(bu)
    assert bu.stage is Stage.WARM


def test_settle_override_passes_with_few_moving(tmp_path):
    # Off target -> a slew + SETTLE happens. With the override, settle passes
    # while a few dishes (3 ≤ 4) are still moving instead of requiring zero.
    eng = _live_engine(tmp_path, observing={"arm_on_dec_ready": True,
                                            "max_moving_antennas": 4})
    bu = BringUp(eng, FakeSite(dec=10.0, state="prepared", fstable_ready=True,
                               not_settled=3),
                 dec_deg=69.04, actor=ACTOR, session_id=SID)
    acts = _actions(bu)
    assert "point_array" in acts and acts[-1] == "utc_start"
    assert bu.stage is Stage.DONE


def test_settle_waits_without_override(tmp_path):
    eng = _live_engine(tmp_path)
    bu = BringUp(eng, FakeSite(dec=10.0, state="prepared", fstable_ready=True,
                               not_settled=3),
                 dec_deg=69.04, actor=ACTOR, session_id=SID)
    _actions(bu)
    assert bu.stage is Stage.SETTLE


def test_override_inherited_from_policy_block(tmp_path):
    # The flag can come purely from the policy `observing:` block (no BringUp
    # kwargs), which is how the supervisor + console autopilot pick it up.
    eng = _live_engine(tmp_path, observing={"arm_on_dec_ready": True})
    bu = BringUp(eng, FakeSite(dec=69.04, state="ready", fstable_ready=True,
                               not_settled=4),   # default max is 4
                 dec_deg=69.04, actor=ACTOR, session_id=SID)
    acts = _actions(bu)
    assert bu.stage is Stage.DONE
    assert acts == ["utc_start"]


def test_describe_mentions_override_when_enabled(tmp_path):
    eng = _live_engine(tmp_path, observing={"arm_on_dec_ready": True,
                                            "max_moving_antennas": 4})
    bu = BringUp(eng, FakeSite(dec=69.04, state="ready", fstable_ready=True),
                 dec_deg=69.04, actor=ACTOR, session_id=SID)
    joined = " | ".join(bu.describe()["steps"])
    assert "dec-ready override" in joined


# -- sequencer / arming -----------------------------------------------------

def _plan_store(engine):
    return PlanStore(engine._shared, engine._shared)


def test_sequencer_ignores_unarmed_plan(engine):
    store = _plan_store(engine)
    store.set(ObservingPlan([Segment(0, 2_000_000_000_000, 69.04, "a")]))
    seq = ObservingSequencer(engine, store, FakeSite(dec=20.0, state="offline"),
                             actor=ACTOR, session_id=SID, now=lambda: 1000.0)
    res = seq.apply()
    assert res.active is False
    assert "not armed" in res.reason


def test_sequencer_runs_armed_plan(engine):
    store = _plan_store(engine)
    store.set(ObservingPlan([Segment(0, 2_000_000_000_000, 69.04, "a")]))
    store.arm(by=ACTOR, now=1.0)
    seq = ObservingSequencer(engine, store, FakeSite(dec=20.0, state="offline"),
                             actor=ACTOR, session_id=SID, now=lambda: 1000.0)
    last = None
    for _ in range(10):
        last = seq.apply()
        if last.stage in ("done", "blocked"):
            break
    assert last.active is True
    assert last.stage == "done"


def test_plan_arm_disarm_roundtrip(engine):
    store = _plan_store(engine)
    store.set(ObservingPlan([Segment(0, 100, 33.0, "x")]))
    assert store.get().armed is False
    p = store.arm(by=ACTOR, now=5.0)
    assert p.armed is True and p.armed_by == ACTOR
    assert store.get().armed is True
    store.disarm()
    assert store.get().armed is False


# -- agent surface ----------------------------------------------------------

def _agent(engine):
    return AgentControl(engine, _plan_store(engine), engine._shared,
                        actor=ACTOR, session_id=SID, tools=None)


def test_compute_transits(engine):
    a = _agent(engine)
    out = a.compute_transits([{"label": "src", "ra_deg": 0.0, "dec_deg": 33.0}],
                             after_unix=1_000_000.0)
    s = out["sources"][0]
    assert s["observable"] is True
    assert s["next_transit_unix"] >= 1_000_000.0
    assert s["next_transit_utc"].endswith("Z")


def test_set_plan_is_staged_then_armed_by_agent(engine):
    a = _agent(engine)
    r = a.observe_at_dec(69.04, label="zenith-ish")
    assert r["armed"] is False
    assert "confirm" in r["next_step"].lower()
    armed = a.arm_observing_plan()
    assert armed["armed"] is True


def test_arm_requires_lease(engine):
    store = _plan_store(engine)
    store.set(ObservingPlan([Segment(0, 100, 33.0, "x")]))   # stage directly
    engine.lease.release(SID)                                # drop the lease
    a = AgentControl(engine, store, engine._shared,
                     actor=ACTOR, session_id=SID, tools=None)
    with pytest.raises(ControlToolError):
        a.arm_observing_plan()
