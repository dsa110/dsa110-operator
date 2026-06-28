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


# -- wider monitoring surface ----------------------------------------------

def test_get_capture_health_flags_kernel_drops(tmp_path):
    cn = CORR_CNS[0]
    data = {
        f"/mon/corr_rt/{cn}/capture/4011": {
            "arm_state": "WRITING", "rate_gbps": 6.1,
            "rate_kernel_drop_pps": 12.0, "degraded": False},
        f"/mon/corr_rt/{cn}/capture/4012": {
            "arm_state": "WRITING", "rate_gbps": 6.0, "degraded": True},
    }
    t = _tools(tmp_path, etcd_data=data)
    out = t.get_capture_health()
    assert out["n_writing"] == 2
    assert out["n_kernel_dropping"] == 1
    assert out["n_degraded"] == 1
    assert out["rate_gbps_max"] == 6.1


def test_get_rfi_detail_rolls_up_flag_fraction(tmp_path):
    data = {
        f"/mon/corr_rt/{CORR_CNS[0]}/rfi": {"total_flag_fraction": {"both": 0.2}},
        f"/mon/corr_rt/{CORR_CNS[1]}/rfi": {"total_flag_fraction": {"both": 0.8}},
    }
    t = _tools(tmp_path, etcd_data=data)
    out = t.get_rfi_detail()
    assert out["n_nodes"] == 2
    assert out["flag_fraction_max"] == 0.8
    assert out["worst_nodes"][0]["cn"] == CORR_CNS[1]


def test_transit_report_in_beam_and_no_catalog(tmp_path):
    data = {"/mon/array/dec": {"dec_deg": 30.0}}
    t = _tools(tmp_path, etcd_data=data)
    out = t.transit_report([
        {"label": "near", "ra_deg": 53.2, "dec_deg": 30.5, "dm_pc_cm3": 26.8},
        {"label": "far", "ra_deg": 10.0, "dec_deg": 45.0},
    ])
    assert out["pointing_dec_deg"] == 30.0
    near, far = out["sources"]
    assert near["in_beam_now"] is True and far["in_beam_now"] is False
    assert near["next_transit_utc"].endswith("Z")
    assert "last_transit_utc" in near
    # no catalog: caller supplied coords; tool just echoes/derives
    assert near["dec_offset_from_pointing_deg"] == 0.5


def test_transit_report_requires_coords(tmp_path):
    t = _tools(tmp_path)
    with pytest.raises(ToolError):
        t.transit_report([{"label": "bad"}])
    with pytest.raises(ToolError):
        t.transit_report([])


def test_health_report_rolls_up_overall(tmp_path):
    cn = CORR_CNS[0]
    data = {f"/mon/service/corr_rt/{c}": {"up": True} for c in CORR_CNS}
    data["/mon/service/search_rt/1"] = {"up": True}
    data[f"/mon/corr_rt/{cn}/capture/4011"] = {
        "arm_state": "WRITING", "rate_gbps": 6.0, "rate_kernel_drop_pps": 5.0}
    dash = FakeDashboard({"/control/system_state": {"state": "Observing",
                                                    "safe_to_arm": True}})
    t = _tools(tmp_path, etcd_data=data, dash=dash)
    out = t.health_report()
    assert "overall" in out and "sections" in out
    assert out["sections"]["capture"]["level"] == "alert"   # kernel drops
    assert out["overall"] == "alert"


def test_get_health_history_aggregates_audit_window(tmp_path):
    import time

    from dsa_operator.audit.log import AuditRecord

    t = _tools(tmp_path)
    now = time.time()

    def rec(**kw):
        kw.setdefault("kind", "system")
        t._audit.record(AuditRecord(**kw))

    # Health edges: warn at 4h ago, alert at 3h ago, back to ok at 1h ago.
    rec(action="health_monitor", ok=True, note="level=warn", ts=now - 4 * 3600,
        result={"level": "warn", "findings": [
            {"level": "warn", "code": "sky_stale", "message": "x"}]})
    rec(action="health_monitor", ok=False, note="level=alert", ts=now - 3 * 3600,
        result={"level": "alert", "findings": [
            {"level": "alert", "code": "corr_nodes_down", "message": "x"}]})
    rec(action="health_monitor", ok=True, note="level=ok", ts=now - 3600,
        result={"level": "ok", "findings": []})

    # Injection probes: one detected, one missed, one shadow-skipped.
    rec(action="injection_health_check.fire", ok=True, mode="live",
        note="outcome=executed", ts=now - 5 * 3600)
    rec(action="injection_health_check.verify", ok=True,
        note="injection probe detected", ts=now - 5 * 3600 + 180,
        result={"baseline_matches": 2, "matches_after": 3})
    rec(action="injection_health_check.fire", ok=True, mode="live",
        note="outcome=executed", ts=now - 2 * 3600)
    rec(action="injection_health_check.verify", ok=False,
        note="injection probe NOT detected within 180s — search may be impaired",
        ts=now - 2 * 3600 + 180,
        result={"baseline_matches": 5, "matches_after": 5})
    rec(action="injection_health_check.fire", ok=True, mode="shadow",
        note="outcome=shadow", ts=now - 1800)
    rec(action="injection_health_check.verify", ok=True,
        note="probe was shadow-only (not sent); skipping detection check",
        ts=now - 1800 + 180, result={"outcome": "shadow"})

    # A control failure in-window (must not double-count the injection miss).
    rec(action="utc_start", kind="control", ok=False, mode="live",
        note="execute failed: HTTP 404", ts=now - 90 * 60)

    # Something OUTSIDE the 6h window must be ignored.
    rec(action="health_monitor", ok=False, note="level=alert", ts=now - 20 * 3600,
        result={"level": "alert", "findings": [
            {"level": "alert", "code": "ancient", "message": "x"}]})

    out = t.get_health_history(6)

    assert out["window_hours"] == 6.0
    h = out["health"]
    assert h["worst_level"] == "alert"
    assert h["n_state_edges"] == 3                     # the 20h-ago one excluded
    assert h["n_alert_edges"] == 1
    assert "corr_nodes_down" in h["alert_codes_seen"]
    assert "ancient" not in h["alert_codes_seen"]
    assert "sky_stale" in h["warn_codes_seen"]

    inj = out["injection_recovery"]
    assert inj["n_fired"] == 3
    assert inj["fired_by_outcome"] == {"executed": 2, "shadow": 1}
    assert inj["detected"] == 1 and inj["missed"] == 1 and inj["shadow_skipped"] == 1
    assert inj["detection_rate"] == 0.5               # 1 / (1 + 1)
    assert inj["last_probe"]["result"] == "shadow"
    assert len(inj["misses"]) == 1
    assert inj["misses"][0]["details"]["matches_after"] == 5

    ctl = out["control_activity"]
    assert ctl["n_control_actions"] == 1
    assert ctl["n_failures"] == 1                      # only utc_start, not the miss
    assert ctl["recent_failures"][0]["action"] == "utc_start"


def test_get_health_history_validates_and_clamps(tmp_path):
    t = _tools(tmp_path)
    with pytest.raises(ToolError):
        t.get_health_history("not-a-number")
    # Out-of-range hours are clamped, not rejected.
    assert t.get_health_history(9999)["window_hours"] == 168.0
    assert t.get_health_history(0)["window_hours"] == 0.1
    # Registered as a real read-only tool and audited.
    from dsa_operator.agent.base import TOOL_SPECS_BY_NAME
    assert "get_health_history" in TOOL_SPECS_BY_NAME
    assert any(r["action"] == "get_health_history" for r in t._audit.tail(10))


def test_describe_monitoring_lists_real_tools(tmp_path):
    from dsa_operator.agent.base import TOOL_SPECS_BY_NAME
    t = _tools(tmp_path)
    cat = t.describe_monitoring()
    referenced = {name for cat_entry in cat.values()
                  for name in cat_entry["tools"]}
    # every advertised tool is a real registered read-only tool
    assert referenced <= set(TOOL_SPECS_BY_NAME)
    assert "health_report" in referenced and "transit_report" in referenced
