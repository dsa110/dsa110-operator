"""Read-only tool surface: composition, policy guard, audit, input validation."""
import pytest

from dsa_operator.audit.log import AuditLog
from dsa_operator.dashboard import DashboardClient
from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd
from dsa_operator.policy import load_policy
from dsa_operator.tools.readonly import CORR_CNS, ReadOnlyTools, ToolError


class FakeDashboard(DashboardClient):
    """DashboardClient with a dict-backed getter (no network)."""

    def __init__(self, responses):
        self._responses = responses
        super().__init__(getter=self._fake_get)

    def _fake_get(self, url, timeout):
        for path, payload in self._responses.items():
            if url.endswith(path):
                return payload
        raise AssertionError(f"unexpected dashboard GET: {url}")


def _tools(tmp_path, *, etcd_data=None, dash=None, actor="tester"):
    etcd = ReadOnlyEtcd(FakeEtcdReader(etcd_data or {}))
    dash = dash or FakeDashboard({})
    audit = AuditLog(tmp_path)
    return ReadOnlyTools(etcd, dash, audit, policy=load_policy(), actor=actor)


def test_get_fleet_status_counts_reporting(tmp_path):
    data = {f"/mon/service/corr_rt/{cn}": {"up": True} for cn in CORR_CNS[:10]}
    data["/mon/service/search_rt/1"] = {"up": True}
    dash = FakeDashboard({"/control/system_state": {"state": "Observing"}})
    t = _tools(tmp_path, etcd_data=data, dash=dash)
    out = t.get_fleet_status()
    assert out["corr"]["n_reporting"] == 10
    assert out["corr"]["n_total"] == 16
    assert out["search"]["n_reporting"] == 1
    assert out["system_state"]["state"] == "Observing"


def test_get_array_pointing_means_and_settle(tmp_path):
    data = {
        "/mon/array/dec": {"dec_deg": 16.27},
        "/mon/ant/1": {"ant_cmd_el": 70.0, "drv_state": 2},
        "/mon/ant/2": {"ant_cmd_el": 72.0, "drv_state": 1},   # still moving
    }
    t = _tools(tmp_path, etcd_data=data)
    out = t.get_array_pointing()
    assert out["target_dec_deg"] == 16.27
    assert out["mean_commanded_el_deg"] == 71.0
    assert out["n_not_settled"] == 1
    assert out["n_antennas_reporting"] == 2


def test_get_mon_requires_mon_prefix(tmp_path):
    t = _tools(tmp_path, etcd_data={"/mon/x": {"v": 1}})
    assert t.get_mon("/mon/x") == {"v": 1}
    with pytest.raises(ToolError):
        t.get_mon("/cmd/corr_rt/0")          # outside /mon
    with pytest.raises(ToolError):
        t.get_mon("/mon/../cmd/x")           # traversal


def test_get_candidate_validates_name(tmp_path):
    dash = FakeDashboard({"/control/recent_events": {"events": [{"name": "260618abcd"}]}})
    t = _tools(tmp_path, dash=dash)
    assert t.get_candidate("260618abcd")["event"]["name"] == "260618abcd"
    with pytest.raises(ToolError):
        t.get_candidate("../../etc/passwd")


def test_query_injections_merges_etcd_and_dashboard(tmp_path):
    data = {
        "/cnf/inject/active/inj1": {"dm_pc_cm3": 500},
        "/mon/dsart/inject/matches/inj1": {"observed_snr": 12.3},
    }
    dash = FakeDashboard({"/control/c2_snapshot": {"inject_match": 1}})
    t = _tools(tmp_path, etcd_data=data, dash=dash)
    out = t.query_injections()
    assert out["active"]["/cnf/inject/active/inj1"]["dm_pc_cm3"] == 500
    assert out["matches"]["/mon/dsart/inject/matches/inj1"]["observed_snr"] == 12.3
    assert out["c2_snapshot"]["inject_match"] == 1


def test_every_call_is_audited(tmp_path):
    data = {"/mon/array/dec": {"dec_deg": 1.0}}
    t = _tools(tmp_path, etcd_data=data)
    t.get_array_pointing()
    t.get_mon("/mon/array/dec")
    actions = [r["action"] for r in t._audit.tail(10)]
    assert "get_array_pointing" in actions
    assert "get_mon" in actions


def test_guard_blocks_non_readonly_action(tmp_path):
    t = _tools(tmp_path)
    # Force a guard on a control action name; must refuse and audit it.
    with pytest.raises(ToolError):
        t._guard("point_array")
    refusals = [r for r in t._audit.tail(10)
                if r["action"] == "point_array" and not r["ok"]]
    assert refusals


def test_dashboard_client_rejects_non_loopback():
    with pytest.raises(ValueError):
        DashboardClient(host="evil.example.com")
