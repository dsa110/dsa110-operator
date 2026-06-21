"""Web control plane: lease, control (shadow), approvals, pause."""
from __future__ import annotations

import pytest

from dsa_operator.agent.stub import StubAgent
from dsa_operator.audit.log import AuditLog
from dsa_operator.control.approvals import ApprovalStore
from dsa_operator.control.engine import ControlEngine
from dsa_operator.control.lease import ExecutorLease
from dsa_operator.dashboard import DashboardClient
from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd
from dsa_operator.etcd.write import FakeOperatorBackend, OperatorEtcdWriter
from dsa_operator.policy import load_policy
from dsa_operator.tools.readonly import ReadOnlyTools
from dsa_operator.web.app import create_app


def _dash_getter(url, timeout):
    return {"ok": True}


class _SharedStore:
    """One dict backing both PlanStore writer + reader (prod shares one etcd)."""

    def __init__(self):
        self._d = {}

    def put(self, key, value, lease_id=None):
        self._d[key] = value

    def delete(self, key):
        self._d.pop(key, None)

    def get_dict(self, key):
        return self._d.get(key)


@pytest.fixture()
def ctx(tmp_path):
    audit = AuditLog(tmp_path / "audit")
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    engine = ControlEngine(load_policy(), ExecutorLease(writer),
                           ApprovalStore(), audit, writer=writer)
    etcd = ReadOnlyEtcd(FakeEtcdReader({}))
    dash = DashboardClient(getter=_dash_getter)
    from dsa_operator.observing.plan import PlanStore
    shared = _SharedStore()
    app = create_app(
        operator="alice",
        tools_factory=lambda a: ReadOnlyTools(etcd, dash, audit, actor=a),
        agent=StubAgent(), audit=audit, control=engine,
        read_etcd=etcd, plan_store=PlanStore(shared, shared),
        secret_key="t",
    )
    app.config.update(TESTING=True)
    return app, engine


def _login(client):
    """Identity is now local (no SSO); kept as a no-op so tests read cleanly."""
    return None


def test_control_denied_without_lease(ctx):
    app, _ = ctx
    c = app.test_client()
    # Monitoring is open locally, but control without the lease is denied.
    d = c.post("/api/control", json={"action": "fire_injection"}).get_json()
    assert d["decision"]["outcome"] == "denied"
    assert c.get("/api/lease").get_json()["data"]["you_hold_it"] is False


def test_lease_acquire_and_control_shadow(ctx):
    app, _ = ctx
    c = app.test_client()
    _login(c)
    # before acquiring, control is denied (no lease)
    d = c.post("/api/control", json={"action": "fire_injection",
                                     "params": {"snr": 10}}).get_json()
    assert d["decision"]["outcome"] == "denied"

    acq = c.post("/api/lease/acquire").get_json()
    assert acq["ok"] is True
    lease = c.get("/api/lease").get_json()["data"]
    assert lease["you_hold_it"] is True
    assert lease["holder"]["actor"] == "alice"

    d2 = c.post("/api/control", json={"action": "fire_injection",
                                      "params": {"snr": 10}}).get_json()
    assert d2["decision"]["outcome"] == "shadow"
    assert d2["decision"]["plan"]["action"] == "fire_injection"


def test_approval_flow_over_http(ctx):
    app, _ = ctx
    c = app.test_client()
    _login(c)
    c.post("/api/lease/acquire")
    # update_fleet_code always needs a human, so it exercises the approval flow.
    params = {"branch": "main"}
    d = c.post("/api/control", json={"action": "update_fleet_code",
                                     "params": params}).get_json()
    assert d["decision"]["outcome"] == "needs_approval"
    # request + grant
    req = c.post("/api/approvals/request",
                 json={"action": "update_fleet_code", "params": params}).get_json()
    ap_id = req["data"]["id"]
    g = c.post(f"/api/approvals/{ap_id}/grant").get_json()
    assert g["data"]["satisfied"] is True
    # now shadow-allowed
    d2 = c.post("/api/control", json={"action": "update_fleet_code",
                                      "params": params}).get_json()
    assert d2["decision"]["outcome"] == "shadow"


def test_pause_blocks_control_and_resume_needs_lease(ctx):
    app, engine = ctx
    c = app.test_client()
    _login(c)
    c.post("/api/lease/acquire")
    assert c.post("/api/pause", json={"reason": "drill"}).get_json()["paused"] is True
    d = c.post("/api/control", json={"action": "fire_injection"}).get_json()
    assert d["decision"]["outcome"] == "denied"
    # resume requires the lease (alice holds it) -> ok
    assert c.post("/api/resume").get_json()["paused"] is False


def test_resume_forbidden_without_lease(ctx):
    app, engine = ctx
    # alice pauses but never takes the lease
    c = app.test_client()
    _login(c)
    c.post("/api/pause")
    r = c.post("/api/resume")
    assert r.status_code == 403


def test_policy_endpoint_lists_gates(ctx):
    app, _ = ctx
    c = app.test_client()
    _login(c)
    data = c.get("/api/policy").get_json()["data"]
    assert data["mode"] in ("shadow", "live")   # operator-controlled
    assert data["actions"]["update_fleet_code"]["gate"] == "approval"
    assert data["actions"]["set_policy"]["two_person"] is True


def test_observability_endpoint(ctx):
    app, _ = ctx
    c = app.test_client()
    _login(c)
    j = c.get("/api/observability?dec=33&ra=50").get_json()["data"]
    assert j["observable"] is True
    assert j["transit_el_deg"] == pytest.approx(85.77, abs=0.01)


def test_plan_set_requires_lease(ctx):
    app, _ = ctx
    c = app.test_client()
    _login(c)
    # no lease yet -> 403
    r = c.post("/api/plan", json={"segments": [
        {"t_start": 0, "t_end": 100, "dec_deg": 33.0, "label": "a"}]})
    assert r.status_code == 403


def test_plan_set_get_and_tick(ctx):
    app, engine = ctx
    c = app.test_client()
    _login(c)
    c.post("/api/lease/acquire")
    r = c.post("/api/plan", json={"segments": [
        {"t_start": 0, "t_end": 2_000_000_000_000, "dec_deg": 44.0, "label": "a"}]})
    assert r.get_json()["data"]["n_segments"] == 1
    got = c.get("/api/plan").get_json()["data"]
    assert got["plan"]["segments"][0]["dec_deg"] == 44.0
    # tick -> point_array through the engine. point_array is autonomous, so in
    # shadow mode it renders the move (shadow) rather than needing approval.
    t = c.post("/api/plan/tick").get_json()["data"]
    assert t["decision"]["action"] == "point_array"
    assert t["decision"]["outcome"] == "shadow"


def test_plan_rejects_bad_envelope(ctx):
    app, _ = ctx
    c = app.test_client()
    _login(c)
    c.post("/api/lease/acquire")
    r = c.post("/api/plan", json={"segments": [
        {"t_start": 0, "t_end": 100, "dec_deg": -40.0, "label": "south"}]})
    assert r.status_code == 400
    assert "invalid plan" in r.get_json()["error"]


def test_takeover_switches_holder(ctx):
    app, engine = ctx
    # someone else holds the lease (emulated via a direct acquire)
    engine.lease.acquire("bob", "sid-bob")
    # the local operator (alice) takes over via http
    ca = app.test_client()
    _login(ca)
    assert ca.post("/api/lease/takeover").get_json()["ok"] is True
    assert engine.lease.holder().actor == "alice"
