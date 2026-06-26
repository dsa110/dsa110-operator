"""Preflight readiness, promotion introspection, the per-turn situation
snapshot, and the sequencer's "SHADOW is not success in live" guard.

These cover the silent-failure class the operator hit: an armed plan that
"runs" while nothing physically happens because mode is shadow, the lease
isn't held, or a bring-up action isn't promoted.
"""
from __future__ import annotations

import dataclasses
import textwrap

from dsa_operator.agent.control import AgentControl
from dsa_operator.audit.log import AuditLog
from dsa_operator.control.approvals import ApprovalStore
from dsa_operator.control.engine import ControlEngine, Decision, Outcome
from dsa_operator.control.executors import (
    FakeControlEtcd, FakeDashboardControl, LiveExecutor)
from dsa_operator.control.lease import ExecutorLease
from dsa_operator.control.preflight import (
    CRITICAL_BRINGUP, observing_preflight, policy_checks)
from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd
from dsa_operator.etcd.write import FakeOperatorBackend, OperatorEtcdWriter
from dsa_operator.observing.plan import PlanStore
from dsa_operator.observing.session import BringUp, Stage
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


def _engine(tmp_path, *, mode="live", promote=None, wire_executor=True,
            hold_lease=True):
    pol = tmp_path / "policy.yaml"
    pol.write_text(textwrap.dedent(f"""
        version: 1
        mode: {mode}
        paused: false
        approval: {{ ttl_seconds: 300, two_person: [] }}
        read_only: [get_fleet_status]
        actions:
          point_array:    {{ target: autonomous, commissioning: autonomous, reversible: true }}
          build_fstable:  {{ target: autonomous, commissioning: autonomous, reversible: true }}
          deploy_fstable: {{ target: autonomous, commissioning: autonomous, reversible: true }}
          start_fleet:    {{ target: autonomous, commissioning: autonomous, reversible: false }}
          restart_all:    {{ target: autonomous, commissioning: autonomous, reversible: false }}
          utc_start:      {{ target: autonomous, commissioning: autonomous, reversible: true }}
        pointing: {{ lat_ovro_deg: 37.23, el_min_deg: 30.0, el_max_deg: 125.0 }}
    """))
    local = tmp_path / "local.yaml"
    local.write_text("promote:\n" + "".join(
        f"  - {a}\n" for a in (promote or [])))
    policy = load_policy(pol, local_path=local)

    writer = OperatorEtcdWriter(FakeOperatorBackend())
    read = ReadOnlyEtcd(FakeEtcdReader({"/mon/array/dec": {"dec_deg": 33.0}}))
    ex = None
    if wire_executor:
        ant = ReadOnlyEtcd(FakeEtcdReader(
            {"/cnf/corr": {"antenna_order": {"0": 1}}}))
        ex = LiveExecutor(dashboard=FakeDashboardControl(),
                          control_etcd=FakeControlEtcd(), read_etcd=ant)
    eng = ControlEngine(policy, ExecutorLease(writer), ApprovalStore(),
                        AuditLog(tmp_path / "a"), writer=writer, read_etcd=read,
                        live_executor=ex)
    if hold_lease:
        eng.lease.acquire(ACTOR, SID)
    shared = _SharedStore()
    return eng, PlanStore(shared, shared), read


# -- policy_checks (config-only) --------------------------------------------

def test_policy_checks_ready_when_live_and_all_promoted(tmp_path):
    eng, _, _ = _engine(tmp_path, mode="live", promote=list(CRITICAL_BRINGUP))
    checks = policy_checks(eng.policy)
    assert all(c["ok"] for c in checks)


def test_policy_checks_flags_shadow_and_unpromoted(tmp_path):
    eng, _, _ = _engine(tmp_path, mode="shadow", promote=["point_array"])
    checks = {c["name"]: c for c in policy_checks(eng.policy)}
    assert checks["policy_mode_live"]["ok"] is False
    assert checks["promote:point_array"]["ok"] is True
    assert checks["promote:utc_start"]["ok"] is False
    assert "config/local.yaml" in checks["promote:utc_start"]["fix"]


# -- observing_preflight (full) ---------------------------------------------

def test_preflight_ready_when_everything_set(tmp_path):
    eng, ps, _ = _engine(tmp_path, mode="live", promote=list(CRITICAL_BRINGUP))
    rep = observing_preflight(eng, session_id=SID, plan_store=ps)
    assert rep["ready_to_observe"] is True
    assert rep["blockers"] == []
    assert rep["bringup_actions_not_promoted"] == []


def test_preflight_blocks_without_lease(tmp_path):
    eng, ps, _ = _engine(tmp_path, mode="live", promote=list(CRITICAL_BRINGUP),
                         hold_lease=False)
    rep = observing_preflight(eng, session_id=SID, plan_store=ps)
    assert rep["ready_to_observe"] is False
    assert any(b.startswith("hold_lease") for b in rep["blockers"])


def test_preflight_blocks_when_action_not_promoted(tmp_path):
    eng, ps, _ = _engine(tmp_path, mode="live",
                         promote=[a for a in CRITICAL_BRINGUP if a != "restart_all"])
    rep = observing_preflight(eng, session_id=SID, plan_store=ps)
    assert rep["ready_to_observe"] is False
    assert "restart_all" in rep["bringup_actions_not_promoted"]
    assert any("restart_all" in b for b in rep["blockers"])


def test_preflight_no_executor_flagged(tmp_path):
    eng, ps, _ = _engine(tmp_path, mode="live", promote=list(CRITICAL_BRINGUP),
                         wire_executor=False)
    rep = observing_preflight(eng, session_id=SID, plan_store=ps)
    assert rep["ready_to_observe"] is False
    assert any(b.startswith("live_executor_wired") for b in rep["blockers"])


# -- list_control_actions exposes promotion ---------------------------------

def test_list_control_actions_reports_will_execute_live(tmp_path):
    eng, ps, read = _engine(tmp_path, mode="live", promote=["point_array"])
    ctrl = AgentControl(eng, ps, read, actor=ACTOR, session_id=SID)
    out = ctrl.list_control_actions()
    assert out["promoted"] == ["point_array"]
    assert out["actions"]["point_array"]["will_execute_live"] is True
    assert out["actions"]["utc_start"]["will_execute_live"] is False
    assert out["actions"]["utc_start"]["promoted"] is False


def test_list_control_actions_shadow_mode_never_live(tmp_path):
    eng, ps, read = _engine(tmp_path, mode="shadow", promote=["point_array"])
    ctrl = AgentControl(eng, ps, read, actor=ACTOR, session_id=SID)
    out = ctrl.list_control_actions()
    assert out["actions"]["point_array"]["will_execute_live"] is False


# -- situation snapshot ------------------------------------------------------

def test_situation_snapshot_reports_mode_and_lease(tmp_path):
    eng, ps, read = _engine(tmp_path, mode="live", promote=list(CRITICAL_BRINGUP))
    ctrl = AgentControl(eng, ps, read, actor=ACTOR, session_id=SID)
    snap = ctrl.situation_snapshot()
    assert "policy mode: live" in snap
    assert "HELD BY YOU" in snap


def test_situation_snapshot_warns_unpromoted_in_live(tmp_path):
    eng, ps, read = _engine(tmp_path, mode="live", promote=["point_array"])
    ctrl = AgentControl(eng, ps, read, actor=ACTOR, session_id=SID)
    snap = ctrl.situation_snapshot()
    assert "NOT promoted" in snap
    assert "utc_start" in snap


def test_situation_snapshot_warns_shadow(tmp_path):
    eng, ps, read = _engine(tmp_path, mode="shadow", promote=list(CRITICAL_BRINGUP))
    ctrl = AgentControl(eng, ps, read, actor=ACTOR, session_id=SID)
    snap = ctrl.situation_snapshot()
    assert "DRY RUN" in snap


# -- sequencer: SHADOW is not success in live (with executor wired) ----------

class _FakeSite:
    def __init__(self, *, dec, state, fstable_ready, not_settled=0):
        self.dec = dec
        self.state = state
        self._fst = fstable_ready
        self.not_settled = not_settled

    def commanded_dec(self):
        return self.dec

    def n_not_settled(self):
        return self.not_settled

    def fleet_state(self):
        return {"state": self.state,
                "safe_to_arm": self.state in ("prepared", "observing")}

    def fstable_status(self, dec_deg):
        return {"all_ready": self._fst}


def test_ok_blocks_shadow_when_live_executor_wired(tmp_path):
    eng, _, _ = _engine(tmp_path, mode="live", promote=[], wire_executor=True)
    bu = BringUp(eng, _FakeSite(dec=33.0, state="offline", fstable_ready=True),
                 dec_deg=33.0, actor=ACTOR, session_id=SID)
    assert bu._ok(Decision(Outcome.EXECUTED, "x", ACTOR)) is True
    assert bu._ok(Decision(Outcome.SHADOW, "x", ACTOR)) is False


def test_ok_allows_shadow_when_no_executor(tmp_path):
    eng, _, _ = _engine(tmp_path, mode="live", promote=[], wire_executor=False)
    bu = BringUp(eng, _FakeSite(dec=33.0, state="offline", fstable_ready=True),
                 dec_deg=33.0, actor=ACTOR, session_id=SID)
    assert bu._ok(Decision(Outcome.SHADOW, "x", ACTOR)) is True


def test_ok_allows_shadow_in_shadow_mode(tmp_path):
    eng, _, _ = _engine(tmp_path, mode="shadow", promote=[], wire_executor=True)
    bu = BringUp(eng, _FakeSite(dec=33.0, state="offline", fstable_ready=True),
                 dec_deg=33.0, actor=ACTOR, session_id=SID)
    assert bu._ok(Decision(Outcome.SHADOW, "x", ACTOR)) is True


def test_bringup_blocks_on_unpromoted_action_in_live(tmp_path):
    # mode=live, executor wired, nothing promoted: the first real action
    # (start_fleet, since we're on-target with fstable present) comes back
    # SHADOW -> the sequencer must BLOCK, not march on to DONE.
    eng, _, _ = _engine(tmp_path, mode="live", promote=[], wire_executor=True)
    bu = BringUp(eng, _FakeSite(dec=33.0, state="offline", fstable_ready=True),
                 dec_deg=33.0, actor=ACTOR, session_id=SID)
    res = bu.run()
    assert res.blocked is True
    assert bu.stage is Stage.BLOCKED
    assert "start_fleet" in bu.reason and "shadow" in bu.reason
