"""Phase 1 web console: auth gating, identity in audit, read-only API, chat."""
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


def _fake_engine(audit):
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    return ControlEngine(load_policy(), ExecutorLease(writer),
                         ApprovalStore(), audit, writer=writer)

ETCD_SEED = {
    "/mon/service/corr_rt/3": {"alive": True},
    "/mon/array/dec": {"dec_deg": 54.5},
    "/mon/ant/1": {"ant_cmd_el": 71.0, "drv_state": 2},
}

DASH_RESPONSES = {
    "/control/system_state": {"state": "OBSERVING"},
    "/sky/status": {"frames": 12, "fresh": True},
    "/api/status": {"chgroups": []},
    "/api/sefd_status": {"ok": True},
    "/control/recent_events": {"events": []},
    "/control/recent_audit": {"rows": []},
    "/control/c2_snapshot": {"armed": False},
}


def _fake_getter(url, timeout):
    path = urlparse(url).path
    if path not in DASH_RESPONSES:
        raise RuntimeError(f"unexpected dashboard path {path}")
    return DASH_RESPONSES[path]


@pytest.fixture()
def app(tmp_path):
    audit = AuditLog(tmp_path / "audit")
    etcd = ReadOnlyEtcd(FakeEtcdReader(ETCD_SEED))
    dash = DashboardClient(getter=_fake_getter)

    def tools_factory(actor):
        return ReadOnlyTools(etcd, dash, audit, actor=actor)

    application = create_app(
        auth=FakeAuth(email="alice@dsa110.org"),
        tools_factory=tools_factory,
        agent=StubAgent(),
        audit=audit,
        control=_fake_engine(audit),
        secret_key="test-secret",
    )
    application.config.update(TESTING=True)
    application._audit = audit  # for assertions
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def _login(client):
    """Drive the FakeAuth SSO round-trip; returns the callback response."""
    r = client.get("/login")
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["Location"]).query)
    state = q["state"][0]
    return client.get(f"/auth/callback?code=fake&state={state}")


# -- unauthenticated --------------------------------------------------------

def test_index_anonymous_shows_login(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Sign in with Google" in r.data


@pytest.mark.parametrize("path", ["/api/fleet", "/api/whoami", "/api/sky"])
def test_api_requires_login(client, path):
    assert client.get(path).status_code == 401


def test_chat_requires_login(client):
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 401


# -- login flow -------------------------------------------------------------

def test_login_sets_identity(client):
    r = _login(client)
    assert r.status_code == 302
    who = client.get("/api/whoami").get_json()
    assert who["user"] == "alice@dsa110.org"


def test_login_denied_when_not_authorized(tmp_path):
    class Deny(FakeAuth):
        def is_authorized(self, email):
            return False

    audit = AuditLog(tmp_path / "audit")
    etcd = ReadOnlyEtcd(FakeEtcdReader(ETCD_SEED))
    dash = DashboardClient(getter=_fake_getter)
    app = create_app(
        auth=Deny(email="mallory@evil.com"),
        tools_factory=lambda a: ReadOnlyTools(etcd, dash, audit, actor=a),
        agent=StubAgent(), audit=audit, control=_fake_engine(audit),
        secret_key="x",
    )
    c = app.test_client()
    r = c.get("/login")
    state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    cb = c.get(f"/auth/callback?code=fake&state={state}")
    assert cb.status_code == 403
    assert c.get("/api/whoami").status_code == 401


def test_bad_state_rejected(client):
    client.get("/login")
    r = client.get("/auth/callback?code=fake&state=tampered")
    assert r.status_code == 401


# -- read-only API ----------------------------------------------------------

def test_fleet_endpoint(client):
    _login(client)
    j = client.get("/api/fleet").get_json()
    assert j["ok"] is True
    assert j["data"]["corr"]["n_reporting"] == 1


def test_pointing_endpoint(client):
    _login(client)
    j = client.get("/api/pointing").get_json()
    assert j["ok"] is True
    assert j["data"]["target_dec_deg"] == 54.5


def test_mon_rejects_out_of_scope_key(client):
    _login(client)
    r = client.get("/api/mon?key=/cnf/secret")
    assert r.status_code == 400


def test_api_calls_are_audited_with_identity(client, app):
    _login(client)
    client.get("/api/fleet")
    rows = app._audit.tail(20)
    fleet_rows = [r for r in rows if r["action"] == "get_fleet_status"]
    assert fleet_rows and fleet_rows[-1]["actor"] == "alice@dsa110.org"


# -- chat -------------------------------------------------------------------

def test_chat_routes_to_tool(client):
    _login(client)
    r = client.post("/api/chat", json={"message": "where is the array pointing?"})
    j = r.get_json()
    assert j["ok"] is True
    assert j["tool_calls"][0]["name"] == "get_array_pointing"
    assert j["model"] == "stub"


def test_chat_empty_message_rejected(client):
    _login(client)
    r = client.post("/api/chat", json={"message": "   "})
    assert r.status_code == 400


def test_chat_is_audited(client, app):
    _login(client)
    client.post("/api/chat", json={"message": "fleet status?"})
    actions = {r["action"] for r in app._audit.tail(30)}
    assert "chat" in actions


# -- mutating-route inventory -----------------------------------------------

def test_mutating_routes_are_exactly_the_known_set(app):
    """No unexpected mutating route may exist; control routes are shadow/gated."""
    allowed_post = {
        "/api/chat", "/logout",
        # Phase 2 control plane (all lease/gate/approval guarded, shadow-only):
        "/api/lease/acquire", "/api/lease/release", "/api/lease/takeover",
        "/api/control", "/api/approvals/request",
        "/api/approvals/<approval_id>/grant", "/api/pause", "/api/resume",
    }
    found = set()
    for rule in app.url_map.iter_rules():
        if rule.methods - {"HEAD", "OPTIONS", "GET"}:
            found.add(rule.rule)
    assert found == allowed_post, f"route drift: {found ^ allowed_post}"
