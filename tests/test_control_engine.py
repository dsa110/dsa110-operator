"""ControlEngine gauntlet: lease, gate, approval, e-stop, shadow-only."""
from __future__ import annotations

import textwrap

import pytest

from dsa_operator.audit.log import AuditLog
from dsa_operator.control.approvals import ApprovalStore
from dsa_operator.control.engine import ControlEngine, Outcome
from dsa_operator.control.lease import ExecutorLease
from dsa_operator.control.verbs import dec_to_el
from dsa_operator.etcd.write import FakeOperatorBackend, OperatorEtcdWriter
from dsa_operator.policy import load_policy

POLICY_YAML = textwrap.dedent("""
    version: 9
    mode: shadow
    paused: false
    approval: { ttl_seconds: 300, two_person: [set_policy] }
    read_only: [get_fleet_status]
    actions:
      fire_injection: { target: autonomous, commissioning: autonomous, reversible: true }
      point_array:    { target: autonomous, commissioning: approval, reversible: true }
      set_policy:     { target: approval,   commissioning: approval, reversible: false }
    pointing: { lat_ovro_deg: 37.23, el_min_deg: 30.0, el_max_deg: 125.0 }
""")

SID = "sid-alice"
ACTOR = "alice@dsa110.org"


@pytest.fixture()
def engine(tmp_path):
    pol_path = tmp_path / "policy.yaml"
    pol_path.write_text(POLICY_YAML)
    policy = load_policy(pol_path, local_path=tmp_path / "none.yaml")
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    audit = AuditLog(tmp_path / "audit")
    eng = ControlEngine(policy, ExecutorLease(writer), ApprovalStore(),
                        audit, writer=writer)
    eng._audit_obj = audit
    return eng


def test_denied_without_lease(engine):
    d = engine.evaluate("fire_injection", {}, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.DENIED
    assert d.reason.startswith("you do not hold")


def test_autonomous_with_lease_runs_shadow(engine):
    engine.lease.acquire(ACTOR, SID)
    d = engine.evaluate("fire_injection", {"snr": 12, "dm": 300},
                        actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.SHADOW
    assert d.plan["action"] == "fire_injection"
    assert d.mode == "shadow"


def test_approval_gate_blocks_then_allows(engine):
    engine.lease.acquire(ACTOR, SID)
    params = {"dec_deg": 33.0}
    d = engine.evaluate("point_array", params, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.NEEDS_APPROVAL
    assert d.extra["required_approvers"] == 1

    ap = engine.approvals.request("point_array", params, requested_by=ACTOR,
                                  n_required=1, ttl_s=300)
    engine.approvals.grant(ap.id, ACTOR)
    d2 = engine.evaluate("point_array", params, actor=ACTOR, session_id=SID)
    assert d2.outcome is Outcome.SHADOW
    assert "el=" in d2.plan["summary"]
    # the approval was single-use; a second attempt needs approval again
    d3 = engine.evaluate("point_array", params, actor=ACTOR, session_id=SID)
    assert d3.outcome is Outcome.NEEDS_APPROVAL


def test_pointing_envelope_enforced(engine):
    engine.lease.acquire(ACTOR, SID)
    # dec way south -> el below floor. Promote point_array? No; instead grant.
    params = {"dec_deg": -40.0}
    ap = engine.approvals.request("point_array", params, requested_by=ACTOR)
    engine.approvals.grant(ap.id, ACTOR)
    d = engine.evaluate("point_array", params, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.DENIED
    assert "envelope" in d.reason


def test_paused_blocks_everything(engine):
    engine.lease.acquire(ACTOR, SID)
    engine.pause(ACTOR, reason="test e-stop")
    assert engine.is_paused()
    d = engine.evaluate("fire_injection", {}, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.DENIED and d.reason.startswith("system is paused")
    engine.resume(ACTOR)
    assert not engine.is_paused()


def test_unknown_action_denied(engine):
    engine.lease.acquire(ACTOR, SID)
    d = engine.evaluate("delete_everything", {}, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.DENIED and "not a known" in d.reason


def test_two_person_needs_two_grants(engine):
    engine.lease.acquire(ACTOR, SID)
    params = {"version": 10}
    d = engine.evaluate("set_policy", params, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.NEEDS_APPROVAL
    assert d.extra["two_person"] is True
    ap = engine.approvals.request("set_policy", params, requested_by=ACTOR,
                                  n_required=2, two_person=True)
    engine.approvals.grant(ap.id, "bob@dsa110.org")
    # still only one approver
    assert engine.evaluate("set_policy", params, actor=ACTOR,
                           session_id=SID).outcome is Outcome.NEEDS_APPROVAL
    engine.approvals.grant(ap.id, "carol@dsa110.org")
    assert engine.evaluate("set_policy", params, actor=ACTOR,
                           session_id=SID).outcome is Outcome.SHADOW


def test_no_live_execution_even_if_mode_live(tmp_path):
    pol_path = tmp_path / "policy.yaml"
    pol_path.write_text(POLICY_YAML.replace("mode: shadow", "mode: live"))
    policy = load_policy(pol_path, local_path=tmp_path / "none.yaml")
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    eng = ControlEngine(policy, ExecutorLease(writer), ApprovalStore(),
                        AuditLog(tmp_path / "a"), writer=writer,
                        live_executor=None)   # no executor in this build
    eng.lease.acquire(ACTOR, SID)
    d = eng.evaluate("fire_injection", {}, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.SHADOW          # never EXECUTED
    assert "no live executor" in d.reason


def test_dec_to_el_formula():
    assert dec_to_el(37.23, 37.23) == pytest.approx(90.0)
    assert dec_to_el(54.23, 37.23) == pytest.approx(107.0)


# -- Phase 3: live execution gated on promotion -----------------------------

def _live_engine(tmp_path, *, mode, promote):
    """Engine with a fake live executor, given a mode + promote list."""
    from dsa_operator.control.executors import (
        FakeControlEtcd,
        FakeDashboardControl,
        LiveExecutor,
    )
    from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd

    pol_path = tmp_path / "policy.yaml"
    pol_path.write_text(POLICY_YAML.replace("mode: shadow", f"mode: {mode}"))
    local = tmp_path / "local.yaml"
    local.write_text("promote: [" + ", ".join(promote) + "]\n")
    policy = load_policy(pol_path, local_path=local)

    writer = OperatorEtcdWriter(FakeOperatorBackend())
    read = ReadOnlyEtcd(FakeEtcdReader(
        {"/cnf/corr": {"antenna_order": {"0": 1, "1": 2, "2": 3}}}))
    dash = FakeDashboardControl()
    ctrl = FakeControlEtcd()
    executor = LiveExecutor(dashboard=dash, control_etcd=ctrl, read_etcd=read)
    eng = ControlEngine(policy, ExecutorLease(writer), ApprovalStore(),
                        AuditLog(tmp_path / "a"), writer=writer,
                        live_executor=executor)
    eng.lease.acquire(ACTOR, SID)
    return eng, dash, ctrl


def test_live_executes_only_when_promoted_and_mode_live(tmp_path):
    eng, dash, ctrl = _live_engine(tmp_path, mode="live", promote=["fire_injection"])
    d = eng.evaluate("fire_injection", {"dm_pc_cm3": 200, "target_snr": 9},
                     actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.EXECUTED
    assert dash.posts and dash.posts[0][0] == "/control/inject"


def test_promoted_but_shadow_mode_stays_shadow(tmp_path):
    eng, dash, ctrl = _live_engine(tmp_path, mode="shadow", promote=["fire_injection"])
    d = eng.evaluate("fire_injection", {}, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.SHADOW
    assert not dash.posts


def test_live_mode_but_unpromoted_stays_shadow(tmp_path):
    eng, dash, ctrl = _live_engine(tmp_path, mode="live", promote=[])
    d = eng.evaluate("fire_injection", {}, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.SHADOW
    assert "not promoted" in d.reason
    assert not dash.posts


def test_live_pointing_writes_cmd_ant(tmp_path):
    eng, dash, ctrl = _live_engine(tmp_path, mode="live", promote=["point_array"])
    params = {"dec_deg": 54.23}
    ap = eng.approvals.request("point_array", params, requested_by=ACTOR)
    eng.approvals.grant(ap.id, ACTOR)
    d = eng.evaluate("point_array", params, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.EXECUTED
    assert len(ctrl.puts) == 3                              # 3 antennas
    assert all(k.startswith("/cmd/ant/") for k, _ in ctrl.puts)


def test_paused_blocks_even_promoted_live(tmp_path):
    eng, dash, ctrl = _live_engine(tmp_path, mode="live", promote=["fire_injection"])
    eng.pause(ACTOR, reason="drill")
    d = eng.evaluate("fire_injection", {}, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.DENIED
    assert not dash.posts                                   # never reached executor
