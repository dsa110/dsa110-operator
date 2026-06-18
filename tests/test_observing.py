"""Phase 4: astronomy helpers, plan model/validation, and the runner."""
from __future__ import annotations

import pytest

from dsa_operator.audit.log import AuditLog
from dsa_operator.control.approvals import ApprovalStore
from dsa_operator.control.engine import ControlEngine, Outcome
from dsa_operator.control.lease import ExecutorLease
from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd
from dsa_operator.etcd.write import FakeOperatorBackend, OperatorEtcdWriter
from dsa_operator.observing import astro
from dsa_operator.observing.plan import ObservingPlan, PlanError, PlanStore, Segment
from dsa_operator.observing.runner import PlanRunner
from dsa_operator.policy import load_policy


# -- astro ------------------------------------------------------------------

def test_dec_to_el_and_back():
    assert astro.dec_to_el(37.23) == pytest.approx(90.0)
    assert astro.dec_to_el(54.23) == pytest.approx(107.0)
    assert astro.el_to_dec(astro.dec_to_el(20.0)) == pytest.approx(20.0)


def test_observable_envelope():
    assert astro.is_observable(37.23)                      # zenith
    assert not astro.is_observable(-30.0)                  # el ~ 22.8 < 30
    assert not astro.is_observable(80.0)                   # el ~ 132.8 > 125


def test_lst_advances_and_wraps():
    t0 = 1_700_000_000.0
    l0 = astro.lst_deg(t0)
    # one sidereal day later, LST returns to ~the same value
    l1 = astro.lst_deg(t0 + 86164.0905)
    assert abs(((l1 - l0 + 180) % 360) - 180) < 0.2


def test_next_transit_self_consistent():
    t0 = 1_700_000_000.0
    ra = 123.4
    tt = astro.next_transit_unix(ra, t0)
    assert tt > t0
    # at transit, LST should equal RA
    assert abs(((astro.lst_deg(tt) - ra + 180) % 360) - 180) < 0.05
    # and it should be within one sidereal day
    assert tt - t0 <= 86400.0


def test_observability_payload():
    o = astro.observability(33.0, ra_deg=50.0, now_unix=1_700_000_000.0)
    assert o.observable is True
    assert o.next_transit_unix is not None
    assert o.to_json()["transit_el_deg"] == pytest.approx(85.77, abs=0.01)


# -- plan model -------------------------------------------------------------

def test_plan_validation_rejects_overlap():
    p = ObservingPlan([
        Segment(0, 100, 33.0, "a"),
        Segment(50, 150, 34.0, "b"),
    ])
    with pytest.raises(PlanError):
        p.validate()


def test_plan_validation_rejects_bad_envelope():
    with pytest.raises(PlanError):
        ObservingPlan([Segment(0, 100, -40.0, "south")]).validate()


def test_plan_active_and_next():
    p = ObservingPlan([
        Segment(0, 100, 33.0, "a"),
        Segment(200, 300, 44.0, "b"),
    ]).validate()
    assert p.dec_at(50) == 33.0
    assert p.active_at(150) is None
    assert p.next_segment(150).label == "b"
    assert p.dec_at(250) == 44.0


def test_plan_roundtrip_json():
    p = ObservingPlan([Segment(0, 100, 33.0, "a", ra_deg=12.3)]).validate()
    p2 = ObservingPlan.from_json(p.to_json())
    assert p2.segments[0].ra_deg == 12.3
    assert p2.segments[0].dec_deg == 33.0


def test_from_sources_centers_on_transit():
    t0 = 1_700_000_000.0
    p = ObservingPlan.from_sources(
        [{"label": "src", "ra_deg": 80.0, "dec_deg": 30.0, "window_min": 20}],
        after_unix=t0, created_by="alice")
    seg = p.segments[0]
    assert seg.label == "src" and seg.ra_deg == 80.0
    assert (seg.t_end - seg.t_start) == pytest.approx(20 * 60)
    mid = (seg.t_start + seg.t_end) / 2
    assert abs(((astro.lst_deg(mid) - 80.0 + 180) % 360) - 180) < 0.05


def test_plan_store_roundtrip():
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    reader = ReadOnlyEtcd(FakeEtcdReader({}))
    # share state: write to operator backend, read via a reader over same store
    store = _SharedStore()
    ps = PlanStore(store, store)
    assert ps.get() is None
    ps.set(ObservingPlan([Segment(0, 100, 33.0, "a")]).validate())
    got = ps.get()
    assert got is not None and got.dec_at(50) == 33.0
    ps.clear()
    assert ps.get() is None


class _SharedStore:
    """Tiny writer+reader sharing one dict, for PlanStore tests."""

    def __init__(self):
        self._d = {}

    def put(self, key, value, lease_id=None):
        self._d[key] = value

    def delete(self, key):
        self._d.pop(key, None)

    def get_dict(self, key):
        return self._d.get(key)


# -- runner -----------------------------------------------------------------

def _engine_with_plan(tmp_path, plan, *, dec_now=None, promote=None, mode="shadow"):
    import textwrap

    pol = tmp_path / "policy.yaml"
    pol.write_text(textwrap.dedent(f"""
        version: 1
        mode: {mode}
        paused: false
        approval: {{ ttl_seconds: 300, two_person: [] }}
        read_only: [get_array_pointing]
        actions:
          point_array: {{ target: autonomous, commissioning: autonomous, reversible: true }}
        pointing: {{ lat_ovro_deg: 37.23, el_min_deg: 30.0, el_max_deg: 125.0 }}
    """))
    local = tmp_path / "local.yaml"
    local.write_text("promote: [" + ", ".join(promote or []) + "]\n")
    policy = load_policy(pol, local_path=local)

    writer = OperatorEtcdWriter(FakeOperatorBackend())
    mon = {"/mon/array/dec": {"dec_deg": dec_now}} if dec_now is not None else {}
    read = ReadOnlyEtcd(FakeEtcdReader(mon))
    store = _SharedStore()
    ps = PlanStore(store, store)
    if plan is not None:
        ps.set(plan)

    from dsa_operator.control.executors import (
        FakeControlEtcd, FakeDashboardControl, LiveExecutor)
    ant_read = ReadOnlyEtcd(FakeEtcdReader(
        {"/cnf/corr": {"antenna_order": {"0": 1, "1": 2}}}))
    ex = LiveExecutor(dashboard=FakeDashboardControl(),
                      control_etcd=FakeControlEtcd(), read_etcd=ant_read)
    eng = ControlEngine(policy, ExecutorLease(writer), ApprovalStore(),
                        AuditLog(tmp_path / "a"), writer=writer,
                        live_executor=ex)
    eng.lease.acquire("alice", "sid")
    runner = PlanRunner(eng, ps, read, actor="alice", session_id="sid")
    return eng, runner


def test_runner_no_plan(tmp_path):
    _, runner = _engine_with_plan(tmp_path, None)
    assert runner.decide(now=10).moved is False


def test_runner_moves_when_off_target(tmp_path):
    plan = ObservingPlan([Segment(0, 1e12, 44.0, "a")]).validate()
    _, runner = _engine_with_plan(tmp_path, plan, dec_now=33.0)
    d = runner.decide(now=10)
    assert d.moved is True and d.target_dec == 44.0
    res = runner.apply(now=10)
    assert res.decision.outcome is Outcome.SHADOW       # autonomous, shadow mode


def test_runner_noop_when_on_target(tmp_path):
    plan = ObservingPlan([Segment(0, 1e12, 44.0, "a")]).validate()
    _, runner = _engine_with_plan(tmp_path, plan, dec_now=44.1)  # within tol
    assert runner.decide(now=10).moved is False


def test_runner_executes_live_when_promoted(tmp_path):
    plan = ObservingPlan([Segment(0, 1e12, 44.0, "a")]).validate()
    _, runner = _engine_with_plan(tmp_path, plan, dec_now=33.0,
                                  promote=["point_array"], mode="live")
    res = runner.apply(now=10)
    assert res.decision.outcome is Outcome.EXECUTED
