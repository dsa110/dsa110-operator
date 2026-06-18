"""Phase 2 web control plane: lease, control (shadow), approvals, pause."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

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
from dsa_operator.web.auth_google import FakeAuth


def _dash_getter(url, timeout):
    return {"ok": True}


@pytest.fixture()
def ctx(tmp_path):
    audit = AuditLog(tmp_path / "audit")
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    engine = ControlEngine(load_policy(), ExecutorLease(writer),
                           ApprovalStore(), audit, writer=writer)
    etcd = ReadOnlyEtcd(FakeEtcdReader({}))
    dash = DashboardClient(getter=_dash_getter)
    app = create_app(
        auth=FakeAuth(email="alice@dsa110.org"),
        tools_factory=lambda a: ReadOnlyTools(etcd, dash, audit, actor=a),
        agent=StubAgent(), audit=audit, control=engine, secret_key="t",
    )
    app.config.update(TESTING=True)
    return app, engine


def _login(client):
    r = client.get("/login")
    state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    client.get(f"/auth/callback?code=fake&state={state}")


def test_control_requires_login(ctx):
    app, _ = ctx
    c = app.test_client()
    assert c.post("/api/control", json={"action": "fire_injection"}).status_code == 401
    assert c.get("/api/lease").status_code == 401


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
    assert lease["holder"]["actor"] == "alice@dsa110.org"

    d2 = c.post("/api/control", json={"action": "fire_injection",
                                      "params": {"snr": 10}}).get_json()
    assert d2["decision"]["outcome"] == "shadow"
    assert d2["decision"]["plan"]["action"] == "fire_injection"


def test_approval_flow_over_http(ctx):
    app, _ = ctx
    c = app.test_client()
    _login(c)
    c.post("/api/lease/acquire")
    params = {"dec_deg": 33.0}
    # gated -> needs approval
    d = c.post("/api/control", json={"action": "point_array",
                                     "params": params}).get_json()
    assert d["decision"]["outcome"] == "needs_approval"
    # request + grant
    req = c.post("/api/approvals/request",
                 json={"action": "point_array", "params": params}).get_json()
    ap_id = req["data"]["id"]
    g = c.post(f"/api/approvals/{ap_id}/grant").get_json()
    assert g["data"]["satisfied"] is True
    # now shadow-allowed
    d2 = c.post("/api/control", json={"action": "point_array",
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
    assert data["mode"] == "shadow"
    assert data["actions"]["update_fleet_code"]["gate"] == "approval"
    assert data["actions"]["set_policy"]["two_person"] is True


def test_takeover_switches_holder(ctx):
    app, engine = ctx
    # bob acquires in one client
    cb = app.test_client()
    _login(cb)  # FakeAuth always returns alice@... -> emulate via direct lease
    engine.lease.acquire("bob@dsa110.org", "sid-bob")
    # alice takes over via http
    ca = app.test_client()
    _login(ca)
    assert ca.post("/api/lease/takeover").get_json()["ok"] is True
    assert engine.lease.holder().actor == "alice@dsa110.org"
