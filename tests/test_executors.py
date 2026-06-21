"""LiveExecutor: plan -> real calls, with fakes (never touches production)."""
from __future__ import annotations

import pytest

from dsa_operator.control.executors import (
    ALLOWED_CONTROL_PREFIXES,
    ExecutorError,
    FakeControlEtcd,
    FakeDashboardControl,
    LiveExecutor,
)
from dsa_operator.control.verbs import get_verb
from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd
from dsa_operator.policy import load_policy

POLICY = load_policy()

ANTENNA_ORDER = {str(i): n for i, n in enumerate([1, 2, 3, 4, 5, 6])}


def _executor():
    read = ReadOnlyEtcd(FakeEtcdReader({"/cnf/corr": {"antenna_order": ANTENNA_ORDER}}))
    dash = FakeDashboardControl()
    ctrl = FakeControlEtcd()
    return LiveExecutor(dashboard=dash, control_etcd=ctrl, read_etcd=read), dash, ctrl


def _plan(action, params):
    return get_verb(action).plan(params, POLICY)


def test_point_array_expands_to_per_antenna_etcd_puts():
    ex, dash, ctrl = _executor()
    plan = _plan("point_array", {"dec_deg": 54.23})
    res = ex.execute(plan, actor="alice@dsa110.org")
    # one put per antenna (6), all to /cmd/ant/<n>, payload cmd=move
    assert len(ctrl.puts) == 6
    keys = {k for k, _ in ctrl.puts}
    assert keys == {f"/cmd/ant/{n}" for n in [1, 2, 3, 4, 5, 6]}
    _, payload = ctrl.puts[0]
    assert payload["cmd"] == "move"
    assert payload["val"] == pytest.approx(107.0)          # 90-(37.23-54.23)
    assert res["results"][0]["n_antennas"] == 6
    assert not dash.posts                                   # no dashboard call


def test_point_array_honours_refants_skip():
    ex, _, ctrl = _executor()
    plan = _plan("point_array", {"dec_deg": 54.23, "refants": [2, 4]})
    ex.execute(plan, actor="a")
    keys = {k for k, _ in ctrl.puts}
    assert keys == {"/cmd/ant/1", "/cmd/ant/3", "/cmd/ant/5", "/cmd/ant/6"}


def test_dashboard_verb_posts_form_with_user():
    ex, dash, ctrl = _executor()
    plan = _plan("stop_fleet", {})
    ex.execute(plan, actor="alice@dsa110.org")
    assert not ctrl.puts
    assert len(dash.posts) == 1
    path, form = dash.posts[0]
    assert path == "/control/stop"
    assert form["confirm"] == "stop"
    assert form["user"] == "alice@dsa110.org"               # actor injected


def test_fire_injection_routes_to_dashboard():
    ex, dash, _ = _executor()
    plan = _plan("fire_injection", {"dm_pc_cm3": 300, "target_snr": 12})
    ex.execute(plan, actor="a")
    path, form = dash.posts[0]
    assert path == "/control/inject"
    assert form["dm_pc_cm3"] == 300 and form["target_snr"] == 12


def test_dumps_enabled_maps_confirm_token():
    ex, dash, _ = _executor()
    ex.execute(_plan("set_dumps_enabled", {"enabled": False}), actor="a")
    _, form = dash.posts[0]
    assert form["enabled"] == "false" and form["confirm"] == "suppress"


def test_control_writer_allowlist_is_only_cmd_ant():
    assert ALLOWED_CONTROL_PREFIXES == ("/cmd/ant/",)
    ctrl = FakeControlEtcd()
    ctrl.put_dict("/cmd/ant/7", {"cmd": "move", "val": 70})
    with pytest.raises(ExecutorError):
        ctrl.put_dict("/cmd/corr_rt/0", {"cmd": "stop"})
    with pytest.raises(ExecutorError):
        ctrl.put_dict("/cnf/spectral_line", {"x": 1})


def test_executor_refuses_local_policy_edit_step():
    ex, _, _ = _executor()
    plan = _plan("set_policy", {"version": 99})
    with pytest.raises(ExecutorError):
        ex.execute(plan, actor="a")


def test_deploy_fstable_rejects_pathy_filename():
    from dsa_operator.control.verbs import VerbError

    with pytest.raises(VerbError):
        _plan("deploy_fstable", {"filename": "../etc/passwd"})


# -- dashboard responses must be checked (silent failures were the bug) ------

def _executor_with(responder):
    read = ReadOnlyEtcd(FakeEtcdReader({"/cnf/corr": {"antenna_order": ANTENNA_ORDER}}))
    dash = FakeDashboardControl(responder=responder)
    return LiveExecutor(dashboard=dash, control_etcd=FakeControlEtcd(),
                        read_etcd=read), dash


def test_dashboard_404_raises_not_silent():
    """A POST to a route the dashboard doesn't expose must fail, not be
    reported as executed (this was the 'armed plan does nothing' bug)."""
    ex, _ = _executor_with(lambda p, f: {"status_code": 404, "ok": False,
                                         "text": "Not Found"})
    with pytest.raises(ExecutorError) as exc:
        ex.execute(_plan("start_fleet", {"dec_deg": 16.27}), actor="a")
    assert "404" in str(exc.value)


def test_dashboard_app_level_refusal_raises():
    """HTTP 200 but {"ok": false} (e.g. utc_start with no captures answering)
    must surface as a failure, not a success."""
    ex, _ = _executor_with(lambda p, f: {
        "status_code": 200, "ok": True,
        "json": {"ok": False, "error": "no captures answering — refusing to "
                                       "broadcast a blind utc_start"}})
    with pytest.raises(ExecutorError) as exc:
        ex.execute(_plan("utc_start", {"margin": 60000}), actor="a")
    assert "no captures answering" in str(exc.value)


def test_dashboard_5xx_raises():
    ex, _ = _executor_with(lambda p, f: {"status_code": 500, "ok": False,
                                         "text": "boom"})
    with pytest.raises(ExecutorError):
        ex.execute(_plan("utc_stop", {}), actor="a")


def test_dashboard_success_with_ok_true_passes():
    ex, dash = _executor_with(lambda p, f: {"status_code": 200, "ok": True,
                                            "json": {"ok": True, "val": 12345}})
    res = ex.execute(_plan("utc_start", {"margin": 60000}), actor="a")
    assert res["results"][0]["result"]["json"]["val"] == 12345
    assert dash.posts[0][0] == "/control/utc_start"
