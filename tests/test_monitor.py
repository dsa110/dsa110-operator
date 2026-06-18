"""Phase 5: health evaluation, recovery playbook, injection check, supervisor."""
from __future__ import annotations

import textwrap

import pytest

from dsa_operator.audit.log import AuditLog
from dsa_operator.control.approvals import ApprovalStore
from dsa_operator.control.engine import ControlEngine, Outcome
from dsa_operator.control.executors import (
    FakeControlEtcd, FakeDashboardControl, LiveExecutor)
from dsa_operator.control.lease import ExecutorLease
from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd
from dsa_operator.etcd.write import FakeOperatorBackend, OperatorEtcdWriter
from dsa_operator.monitor.health import (
    LEVEL_ALERT, LEVEL_OK, LEVEL_WARN, HealthReport, HealthThresholds,
    evaluate_health)
from dsa_operator.monitor.injection import InjectionHealthCheck
from dsa_operator.monitor.recovery import RecoveryPlaybook
from dsa_operator.monitor.supervisor import AutonomyConfig, AutonomySupervisor
from dsa_operator.observing.plan import ObservingPlan, PlanStore, Segment
from dsa_operator.observing.runner import PlanRunner
from dsa_operator.policy import load_policy


# -- fakes ------------------------------------------------------------------

class FakeTools:
    """Stand-in for ReadOnlyTools returning canned, mutable telemetry."""

    def __init__(self, *, corr_rep=16, search_rep=16, sky_age=10.0,
                 sefd_age=100.0, now=1000.0):
        self.now = now
        self._corr_rep = corr_rep
        self._search_rep = search_rep
        self._sky_age = sky_age
        self._sefd_age = sefd_age
        self.matches = {}

    def get_fleet_status(self):
        return {"corr": {"n_reporting": self._corr_rep, "n_total": 16,
                         "down": [] if self._corr_rep == 16 else ["corr05"]},
                "search": {"n_reporting": self._search_rep, "n_total": 16,
                           "down": [] if self._search_rep == 16 else ["search03"]}}

    def get_sky_status(self):
        return {"latest_frame_unix": self.now - self._sky_age}

    def get_sefd(self):
        return {"last_update": self.now - self._sefd_age}

    def query_injections(self):
        return {"matches": dict(self.matches)}


class _FakeObs:
    def __init__(self, overrun=False, elapsed_s=0.0, cap=0):
        self.overrun = overrun
        self.elapsed_s = elapsed_s
        self.max_obs_seconds = cap


def _engine(tmp_path, *, mode="shadow", promote=None, authority=None):
    pol = tmp_path / "policy.yaml"
    pol.write_text(textwrap.dedent(f"""
        version: 1
        mode: {mode}
        paused: false
        approval: {{ ttl_seconds: 300, two_person: [] }}
        read_only: [get_fleet_status]
        actions:
          fire_injection: {{ target: autonomous, commissioning: autonomous, reversible: true }}
          bounce_search:  {{ target: autonomous, commissioning: autonomous, reversible: true }}
          point_array:    {{ target: autonomous, commissioning: autonomous, reversible: true }}
        pointing: {{ lat_ovro_deg: 37.23, el_min_deg: 30.0, el_max_deg: 125.0 }}
    """))
    local = tmp_path / "local.yaml"
    local.write_text("promote: [" + ", ".join(promote or []) + "]\n")
    policy = load_policy(pol, local_path=local)

    writer = OperatorEtcdWriter(FakeOperatorBackend())
    mon = {}
    if authority is not None:
        mon["/cmd/operator/control"] = authority
    read = ReadOnlyEtcd(FakeEtcdReader(mon))
    ant_read = ReadOnlyEtcd(FakeEtcdReader(
        {"/cnf/corr": {"antenna_order": {"0": 1, "1": 2}}}))
    ex = LiveExecutor(dashboard=FakeDashboardControl(),
                      control_etcd=FakeControlEtcd(), read_etcd=ant_read)
    eng = ControlEngine(policy, ExecutorLease(writer), ApprovalStore(),
                        AuditLog(tmp_path / "a"), writer=writer, read_etcd=read,
                        live_executor=ex)
    return eng


# -- health -----------------------------------------------------------------

def test_health_all_ok():
    rep = evaluate_health(FakeTools(), now=1000.0)
    assert rep.level == LEVEL_OK
    assert not rep.alerts


def test_health_flags_nodes_down():
    rep = evaluate_health(FakeTools(corr_rep=15, search_rep=12), now=1000.0)
    assert rep.level == LEVEL_ALERT
    codes = rep.codes
    assert "corr_nodes_down" in codes and "search_nodes_down" in codes


def test_health_warns_on_stale_sky():
    rep = evaluate_health(FakeTools(sky_age=999.0),
                          thresholds=HealthThresholds(sky_frame_max_age_s=300),
                          now=1000.0)
    assert rep.level == LEVEL_WARN
    assert "sky_stale" in rep.codes


def test_health_observation_overrun_alerts():
    rep = evaluate_health(FakeTools(), now=1000.0,
                          observation=_FakeObs(overrun=True, elapsed_s=99, cap=60))
    assert "obs_overrun" in rep.codes
    assert rep.level == LEVEL_ALERT


# -- recovery ---------------------------------------------------------------

def test_recovery_proposes_bounce_for_search_down():
    rep = evaluate_health(FakeTools(search_rep=10), now=1000.0)
    props = RecoveryPlaybook().propose(rep)
    assert len(props) == 1
    assert props[0].action == "bounce_search" and props[0].auto is True


def test_recovery_no_proposal_for_corr_down():
    rep = evaluate_health(FakeTools(corr_rep=10), now=1000.0)
    assert RecoveryPlaybook().propose(rep) == []


def test_recovery_no_proposal_when_healthy():
    rep = evaluate_health(FakeTools(), now=1000.0)
    assert RecoveryPlaybook().propose(rep) == []


# -- injection --------------------------------------------------------------

def test_injection_shadow_skips_detection(tmp_path):
    eng = _engine(tmp_path, mode="shadow")
    eng.lease.acquire("alice", "sid")
    tools = FakeTools()
    chk = InjectionHealthCheck(eng, tools, eng.audit, session_id="sid",
                               verify_after_s=100.0)
    d = chk.fire(now=0.0)
    assert d.outcome is Outcome.SHADOW and chk.in_flight
    assert chk.due_to_verify(now=200.0)
    res = chk.verify(now=200.0)
    assert res.ok and res.code == "shadow_probe"


def test_injection_live_detects_increase(tmp_path):
    eng = _engine(tmp_path, mode="live", promote=["fire_injection"])
    eng.lease.acquire("alice", "sid")
    tools = FakeTools()
    chk = InjectionHealthCheck(eng, tools, eng.audit, session_id="sid",
                               verify_after_s=100.0)
    d = chk.fire(now=0.0)
    assert d.outcome is Outcome.EXECUTED
    tools.matches = {"m1": {"snr": 30}}        # a match landed
    res = chk.verify(now=200.0)
    assert res.ok and res.code == "injection_detected"


def test_injection_live_misses_when_no_match(tmp_path):
    eng = _engine(tmp_path, mode="live", promote=["fire_injection"])
    eng.lease.acquire("alice", "sid")
    chk = InjectionHealthCheck(eng, FakeTools(), eng.audit, session_id="sid",
                               verify_after_s=100.0)
    chk.fire(now=0.0)
    res = chk.verify(now=200.0)
    assert not res.ok and res.code == "injection_missed"


# -- supervisor -------------------------------------------------------------

def _supervisor(eng, tools, cfg, *, plan_runner=None, injection=None,
                session_id="supervisor"):
    return AutonomySupervisor(eng, tools, eng.audit, cfg,
                              plan_runner=plan_runner, injection=injection,
                              session_id=session_id)


def test_supervisor_disabled_is_noop(tmp_path):
    eng = _engine(tmp_path)
    sup = _supervisor(eng, FakeTools(), AutonomyConfig(enabled=False))
    tick = sup.tick(now=1000.0)
    assert tick.health is None and "supervisor disabled" in tick.notes


def test_supervisor_monitors_but_gates_without_lease(tmp_path):
    eng = _engine(tmp_path)
    cfg = AutonomyConfig(enabled=True, auto_recover=True, health_s=1.0)
    sup = _supervisor(eng, FakeTools(search_rep=10), cfg)
    tick = sup.tick(now=1000.0)
    assert tick.health is not None                 # monitoring ran
    assert tick.gated_out and "lease" in tick.gate_reason
    assert tick.recoveries == []                    # no mutation without lease


def test_supervisor_auto_recovers_with_lease(tmp_path):
    eng = _engine(tmp_path, mode="live", promote=["bounce_search"])
    eng.lease.acquire("agent", "supervisor")
    cfg = AutonomyConfig(enabled=True, auto_recover=True, health_s=1.0)
    sup = _supervisor(eng, FakeTools(search_rep=10), cfg)
    tick = sup.tick(now=1000.0)
    assert not tick.gated_out
    assert tick.recoveries and tick.recoveries[0]["action"] == "bounce_search"
    assert tick.recoveries[0]["outcome"] == "executed"


def test_supervisor_locked_out_by_dashboard(tmp_path):
    eng = _engine(tmp_path, mode="live", promote=["bounce_search"],
                  authority={"agents_enabled": False})
    eng.lease.acquire("agent", "supervisor")
    cfg = AutonomyConfig(enabled=True, auto_recover=True, health_s=1.0)
    sup = _supervisor(eng, FakeTools(search_rep=10), cfg)
    tick = sup.tick(now=1000.0)
    assert tick.gated_out and "locked out" in tick.gate_reason
    assert tick.recoveries == []


def test_supervisor_ticks_plan(tmp_path):
    eng = _engine(tmp_path, mode="live", promote=["point_array"])
    eng.lease.acquire("agent", "supervisor")
    store = type("S", (), {"_d": {}, "put": lambda s, k, v, lease_id=None: s._d.__setitem__(k, v),
                           "delete": lambda s, k: s._d.pop(k, None),
                           "get_dict": lambda s, k: s._d.get(k)})()
    ps = PlanStore(store, store)
    ps.set(ObservingPlan([Segment(0, 1e12, 44.0, "a")]).validate())
    read = ReadOnlyEtcd(FakeEtcdReader({"/mon/array/dec": {"dec_deg": 33.0}}))
    runner = PlanRunner(eng, ps, read, actor="agent", session_id="supervisor")
    cfg = AutonomyConfig(enabled=True, run_plan=True, plan_s=1.0)
    sup = _supervisor(eng, FakeTools(), cfg, plan_runner=runner)
    tick = sup.tick(now=1000.0)
    assert tick.plan is not None and tick.plan["moved"] is True


def test_supervisor_respects_health_cadence(tmp_path):
    eng = _engine(tmp_path)
    eng.lease.acquire("agent", "supervisor")
    cfg = AutonomyConfig(enabled=True, health_s=60.0)
    sup = _supervisor(eng, FakeTools(), cfg)
    t0 = sup.tick(now=1000.0)
    assert "health" in t0.ran
    t1 = sup.tick(now=1010.0)          # < 60 s later
    assert "health" not in t1.ran      # not due yet
    t2 = sup.tick(now=1100.0)          # > 60 s later
    assert "health" in t2.ran


def test_autonomy_config_from_policy():
    pol = load_policy()                # the shipped config/policy.yaml
    cfg = AutonomyConfig.from_policy(pol)
    assert cfg.enabled is False        # safe default
    assert cfg.health_s == 60.0
    assert cfg.min_interval_s == 30.0  # min(60, 3600, 30)
