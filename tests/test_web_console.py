"""Web console: local identity, identity in audit, read-only API, chat."""
from __future__ import annotations

from urllib.parse import urlparse

import pytest

from dsa_operator.agent.stub import StubAgent
from dsa_operator.audit.log import AuditLog, AuditRecord
from dsa_operator.control.approvals import ApprovalStore
from dsa_operator.control.engine import ControlEngine
from dsa_operator.control.lease import ExecutorLease
from dsa_operator.dashboard import DashboardClient
from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd
from dsa_operator.etcd.write import FakeOperatorBackend, OperatorEtcdWriter
from dsa_operator.policy import load_policy
from dsa_operator.tools.readonly import ReadOnlyTools
from dsa_operator.web.app import create_app


def _fake_engine(audit):
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    return ControlEngine(load_policy(), ExecutorLease(writer),
                         ApprovalStore(), audit, writer=writer)


def _fake_plan_store(engine, read_etcd):
    from dsa_operator.observing.plan import PlanStore
    return PlanStore(engine._writer, read_etcd)

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

    engine = _fake_engine(audit)
    application = create_app(
        operator="alice",
        tools_factory=tools_factory,
        agent=StubAgent(),
        audit=audit,
        control=engine,
        read_etcd=etcd,
        plan_store=_fake_plan_store(engine, etcd),
        secret_key="test-secret",
    )
    application.config.update(TESTING=True)
    application._audit = audit  # for assertions
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


# -- local identity (no SSO) ------------------------------------------------

def test_index_shows_console(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Sign in" not in r.data        # no login page anymore


def test_identity_from_operator_arg(client):
    who = client.get("/api/whoami").get_json()
    assert who["user"] == "alice"


def test_identity_defaults_to_env_user(tmp_path, monkeypatch):
    monkeypatch.setenv("DSA_OPERATOR_USER", "casey")
    audit = AuditLog(tmp_path / "audit")
    etcd = ReadOnlyEtcd(FakeEtcdReader(ETCD_SEED))
    dash = DashboardClient(getter=_fake_getter)
    engine = _fake_engine(audit)
    app = create_app(
        tools_factory=lambda a: ReadOnlyTools(etcd, dash, audit, actor=a),
        agent=StubAgent(), audit=audit, control=engine,
        read_etcd=etcd, plan_store=_fake_plan_store(engine, etcd),
        secret_key="test-secret")
    app.config.update(TESTING=True)
    assert app.test_client().get("/api/whoami").get_json()["user"] == "casey"


def test_no_login_routes(app):
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/login" not in rules and "/auth/callback" not in rules


# -- etcd/dashboard endpoint resolution (laptop tunnel vs on-h23 direct) -----

def test_etcd_endpoint_defaults_to_loopback(monkeypatch):
    from dsa_operator.web import app as appmod
    monkeypatch.delenv("DSA_OPERATOR_ETCD_HOST", raising=False)
    monkeypatch.delenv("DSA_OPERATOR_ETCD_PORT", raising=False)
    assert appmod._etcd_host() == "127.0.0.1"          # the SSH-tunnel default
    assert appmod._etcd_port() == 12379


def test_etcd_endpoint_direct_on_h23(monkeypatch):
    from dsa_operator.web import app as appmod
    monkeypatch.setenv("DSA_OPERATOR_ETCD_HOST", "etcdv3service.pro.pvt")
    monkeypatch.setenv("DSA_OPERATOR_ETCD_PORT", "2379")
    monkeypatch.setenv("DSA_OPERATOR_DASHBOARD_PORT", "5778")
    assert appmod._etcd_host() == "etcdv3service.pro.pvt"
    assert appmod._etcd_port() == 2379
    assert appmod._dash_port() == 5778


# -- read-only API ----------------------------------------------------------

def test_fleet_endpoint(client):
    j = client.get("/api/fleet").get_json()
    assert j["ok"] is True
    assert j["data"]["corr"]["n_reporting"] == 1


def test_pointing_endpoint(client):
    j = client.get("/api/pointing").get_json()
    assert j["ok"] is True
    assert j["data"]["target_dec_deg"] == 54.5


def test_status_endpoint_rollup(client):
    """The status bar's one-shot poll: mode, e-stop, system_state, pointing,
    lease, plan — all present and read from the live tools."""
    j = client.get("/api/status").get_json()
    assert j["ok"] is True
    d = j["data"]
    assert d["mode"] in ("shadow", "live")   # operator-controlled
    assert d["paused"] is False
    assert d["system_state"]["state"] == "OBSERVING"
    assert d["pointing"]["target_dec_deg"] == 54.5
    assert d["pointing"]["n_not_settled"] == 0     # /mon/ant/1 drv_state=2 == settled
    assert "lease" in d and "plan" in d


def test_status_includes_observing_field(client):
    """The status roll-up carries the live bring-up state (None until the
    autopilot has run a tick), so the UI can show a BLOCKED/waiting pill."""
    d = client.get("/api/status").get_json()["data"]
    assert "observing" in d


def test_activity_feed_returns_recent_actions_newest_first(client, app):
    # generate a couple of audited reads, then a control failure row
    client.get("/api/fleet")
    client.get("/api/pointing")
    app._audit.record(AuditRecord(
        action="utc_start", kind="control", actor="alice", ok=False,
        mode="live", note="dashboard POST /control/utc_start refused: "
                          "no captures answering"))
    j = client.get("/api/activity?n=10").get_json()
    assert j["ok"] is True
    rows = j["data"]
    assert rows[0]["action"] == "utc_start"        # newest first
    assert rows[0]["ok"] is False


def test_activity_feed_failures_only(client, app):
    client.get("/api/fleet")                        # ok read
    app._audit.record(AuditRecord(action="utc_start", kind="control",
                                  actor="alice", ok=False, mode="live",
                                  note="execute failed: HTTP 404"))
    rows = client.get("/api/activity?failures=1").get_json()["data"]
    assert rows
    assert all(r["ok"] is False for r in rows)
    assert rows[0]["action"] == "utc_start"


def test_mon_rejects_out_of_scope_key(client):
    r = client.get("/api/mon?key=/cnf/secret")
    assert r.status_code == 400


def test_api_calls_are_audited_with_identity(client, app):
    client.get("/api/fleet")
    rows = app._audit.tail(20)
    fleet_rows = [r for r in rows if r["action"] == "get_fleet_status"]
    assert fleet_rows and fleet_rows[-1]["actor"] == "alice"


# -- chat -------------------------------------------------------------------

def test_chat_routes_to_tool(client):
    r = client.post("/api/chat", json={"message": "where is the array pointing?"})
    j = r.get_json()
    assert j["ok"] is True
    assert j["tool_calls"][0]["name"] == "get_array_pointing"
    assert j["model"] == "stub"


def test_chat_empty_message_rejected(client):
    r = client.post("/api/chat", json={"message": "   "})
    assert r.status_code == 400


def test_chat_is_audited(client, app):
    client.post("/api/chat", json={"message": "fleet status?"})
    actions = {r["action"] for r in app._audit.tail(30)}
    assert "chat" in actions


# -- mutating-route inventory -----------------------------------------------

def test_mutating_routes_are_exactly_the_known_set(app):
    """No unexpected mutating route may exist; control routes are shadow/gated."""
    allowed_post = {
        "/api/chat",
        # Phase 2 control plane (all lease/gate/approval guarded, shadow-only):
        "/api/lease/acquire", "/api/lease/release", "/api/lease/takeover",
        "/api/control", "/api/approvals/request",
        "/api/approvals/<approval_id>/grant", "/api/pause", "/api/resume",
            # Phase 4 observing plan (lease-gated; pointing still flows the engine):
            "/api/plan", "/api/plan/clear", "/api/plan/tick", "/api/plan/preview",
            "/api/plan/sequence", "/api/plan/step", "/api/plan/arm", "/api/plan/disarm",
            # Phase 5 autonomy (monitor-only from the web; mutations gated by lease):
            "/api/autonomy/tick",
        }
    found = set()
    for rule in app.url_map.iter_rules():
        if rule.methods - {"HEAD", "OPTIONS", "GET"}:
            found.add(rule.rule)
    assert found == allowed_post, f"route drift: {found ^ allowed_post}"
