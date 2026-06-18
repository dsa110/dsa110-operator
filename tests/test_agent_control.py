"""Phase 6: the agent's control surface (propose/approve/plan), gated."""
from __future__ import annotations

import textwrap

from dsa_operator.agent.control import (
    CONTROL_SPECS_BY_NAME, AgentControl, ControlToolError)
from dsa_operator.agent.stub import StubAgent
from dsa_operator.audit.log import AuditLog
from dsa_operator.control.approvals import ApprovalStore
from dsa_operator.control.engine import ControlEngine
from dsa_operator.control.executors import (
    FakeControlEtcd, FakeDashboardControl, LiveExecutor)
from dsa_operator.control.lease import ExecutorLease
from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd
from dsa_operator.etcd.write import FakeOperatorBackend, OperatorEtcdWriter
from dsa_operator.observing.plan import ObservingPlan, PlanStore, Segment
from dsa_operator.policy import load_policy


class _SharedStore:
    def __init__(self):
        self._d = {}

    def put(self, key, value, lease_id=None):
        self._d[key] = value

    def delete(self, key):
        self._d.pop(key, None)

    def get_dict(self, key):
        return self._d.get(key)


def _control(tmp_path, *, mode="shadow", promote=None, dec_now=33.0,
             hold_lease=True, sid="sid", actor="alice"):
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
          point_array:    {{ target: autonomous, commissioning: approval, reversible: true }}
        pointing: {{ lat_ovro_deg: 37.23, el_min_deg: 30.0, el_max_deg: 125.0 }}
    """))
    local = tmp_path / "local.yaml"
    local.write_text("promote: [" + ", ".join(promote or []) + "]\n")
    policy = load_policy(pol, local_path=local)

    writer = OperatorEtcdWriter(FakeOperatorBackend())
    read = ReadOnlyEtcd(FakeEtcdReader({"/mon/array/dec": {"dec_deg": dec_now}}))
    ant_read = ReadOnlyEtcd(FakeEtcdReader(
        {"/cnf/corr": {"antenna_order": {"0": 1, "1": 2}}}))
    ex = LiveExecutor(dashboard=FakeDashboardControl(),
                      control_etcd=FakeControlEtcd(), read_etcd=ant_read)
    eng = ControlEngine(policy, ExecutorLease(writer), ApprovalStore(),
                        AuditLog(tmp_path / "a"), writer=writer, read_etcd=read,
                        live_executor=ex)
    if hold_lease:
        eng.lease.acquire(actor, sid)
    store = _SharedStore()
    ps = PlanStore(store, store)
    ctrl = AgentControl(eng, ps, read, actor=actor, session_id=sid)
    return eng, ctrl


# -- introspection ----------------------------------------------------------

def test_list_control_actions_exposes_gates(tmp_path):
    _, ctrl = _control(tmp_path)
    out = ctrl.list_control_actions()
    assert out["actions"]["point_array"]["gate"] == "approval"
    assert out["actions"]["fire_injection"]["gate"] == "autonomous"


def test_lease_status_reflects_holder(tmp_path):
    _, ctrl = _control(tmp_path, hold_lease=True)
    assert ctrl.lease_status()["i_hold"] is True
    _, ctrl2 = _control(tmp_path, hold_lease=False)
    assert ctrl2.lease_status()["i_hold"] is False


# -- propose_action ---------------------------------------------------------

def test_propose_denied_without_lease(tmp_path):
    _, ctrl = _control(tmp_path, hold_lease=False)
    d = ctrl.propose_action("fire_injection", {})
    assert d["outcome"] == "denied"
    assert "lease" in d["next_step"].lower()


def test_propose_autonomous_shadow(tmp_path):
    _, ctrl = _control(tmp_path, mode="shadow")
    d = ctrl.propose_action("fire_injection", {})
    assert d["outcome"] == "shadow"


def test_propose_autonomous_executes_when_promoted(tmp_path):
    _, ctrl = _control(tmp_path, mode="live", promote=["bounce_search"])
    d = ctrl.propose_action("bounce_search", {})
    assert d["outcome"] == "executed"


def test_propose_approval_then_grant(tmp_path):
    eng, ctrl = _control(tmp_path, mode="shadow")
    d = ctrl.propose_action("point_array", {"dec_deg": 44.0})
    assert d["outcome"] == "needs_approval"
    # the agent may request, but a human grants
    req = ctrl.request_approval("point_array", {"dec_deg": 44.0})
    eng.approvals.grant(req["id"], "bob")
    d2 = ctrl.propose_action("point_array", {"dec_deg": 44.0})
    assert d2["outcome"] == "shadow"


def test_agent_cannot_grant_its_own_request(tmp_path):
    _, ctrl = _control(tmp_path)
    # AgentControl exposes no grant method at all.
    assert not hasattr(ctrl, "grant")
    assert "grant" not in CONTROL_SPECS_BY_NAME


def test_propose_unknown_action_raises(tmp_path):
    _, ctrl = _control(tmp_path)
    d = ctrl.propose_action("rm_rf_slash", {})
    assert d["outcome"] == "denied"          # engine: unknown_action


# -- observing plan ---------------------------------------------------------

def test_set_plan_requires_lease(tmp_path):
    _, ctrl = _control(tmp_path, hold_lease=False)
    try:
        ctrl.set_observing_plan(segments=[{"t_start": 0, "t_end": 1e12,
                                           "dec_deg": 44.0, "label": "a"}])
        assert False, "expected ControlToolError"
    except ControlToolError as exc:
        assert "lease" in str(exc)


def test_set_and_tick_plan(tmp_path):
    _, ctrl = _control(tmp_path, mode="live", promote=["point_array"],
                       dec_now=33.0)
    out = ctrl.set_observing_plan(segments=[{"t_start": 0, "t_end": 1e12,
                                             "dec_deg": 44.0, "label": "a"}])
    assert out["n_segments"] == 1
    tick = ctrl.tick_plan()
    assert tick["moved"] is True
    assert tick["decision"]["outcome"] == "executed"


# -- tool registry wiring ---------------------------------------------------

def test_control_specs_invoke_methods(tmp_path):
    _, ctrl = _control(tmp_path)
    assert CONTROL_SPECS_BY_NAME["lease_status"].invoke(ctrl, {})["i_hold"] is True
    out = CONTROL_SPECS_BY_NAME["propose_action"].invoke(
        ctrl, {"action": "fire_injection", "params": {}})
    assert out["outcome"] in ("shadow", "executed")


# -- stub routing -----------------------------------------------------------

def test_stub_routes_lease_question(tmp_path):
    _, ctrl = _control(tmp_path)
    resp = StubAgent().chat("who holds the lease?", actor="alice",
                            tools=None, control=ctrl)
    assert resp.tool_calls[0].name == "lease_status"


def test_stub_routes_capability_question(tmp_path):
    _, ctrl = _control(tmp_path)
    resp = StubAgent().chat("what can you control or do?", actor="alice",
                            tools=None, control=ctrl)
    assert resp.tool_calls[0].name == "list_control_actions"


# -- claude dispatch (no network) -------------------------------------------

def test_claude_dispatches_control_tool(tmp_path):
    from dsa_operator.agent.claude import ClaudeAgent
    agent = ClaudeAgent.__new__(ClaudeAgent)   # bypass __init__ (no anthropic)
    _, ctrl = _control(tmp_path)

    class _Block:
        type = "tool_use"
        name = "lease_status"
        input: dict = {}
        id = "x"

    call, payload = agent._run_tool(_Block(), tools=None, control=ctrl)
    assert call.ok and payload["i_hold"] is True

    class _Bad(_Block):
        name = "propose_action"
        input = {"action": "fire_injection", "params": {}}

    call2, payload2 = agent._run_tool(_Bad(), tools=None, control=ctrl)
    assert call2.ok and payload2["outcome"] in ("shadow", "executed")
