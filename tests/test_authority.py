"""Dashboard authority over the agent: lockout, executor pin, obs watchdog."""
from __future__ import annotations

import textwrap

import pytest

from dsa_operator.audit.log import AuditLog
from dsa_operator.control.approvals import ApprovalStore
from dsa_operator.control.authority import (
    AUTHORITY_KEY,
    observation_status,
    read_authority,
)
from dsa_operator.control.engine import ControlEngine, Outcome
from dsa_operator.control.lease import ExecutorLease
from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd
from dsa_operator.etcd.write import FakeOperatorBackend, OperatorEtcdWriter
from dsa_operator.observing import astro
from dsa_operator.policy import load_policy

POLICY_YAML = textwrap.dedent("""
    version: 1
    mode: shadow
    paused: false
    approval: { ttl_seconds: 300, two_person: [] }
    read_only: [get_fleet_status]
    actions:
      fire_injection: { target: autonomous, commissioning: autonomous, reversible: true }
    pointing: { lat_ovro_deg: 37.23, el_min_deg: 30.0, el_max_deg: 125.0 }
""")

SID, ACTOR = "sid-a", "alice@dsa110.org"


def _engine(tmp_path, etcd_seed):
    pol = tmp_path / "policy.yaml"
    pol.write_text(POLICY_YAML)
    policy = load_policy(pol, local_path=tmp_path / "none.yaml")
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    read = ReadOnlyEtcd(FakeEtcdReader(etcd_seed))
    eng = ControlEngine(policy, ExecutorLease(writer), ApprovalStore(),
                        AuditLog(tmp_path / "a"), writer=writer, read_etcd=read)
    eng.lease.acquire(ACTOR, SID)
    return eng


# -- authority parsing ------------------------------------------------------

def test_default_authority_is_open():
    a = read_authority(ReadOnlyEtcd(FakeEtcdReader({})))
    assert a.agents_enabled and a.executor_email is None and a.max_obs_seconds is None


def test_authority_parsing():
    a = read_authority(ReadOnlyEtcd(FakeEtcdReader({
        AUTHORITY_KEY: {"agents_enabled": False, "executor_email": "bob@x",
                        "max_obs_seconds": 3600}})))
    assert a.agents_enabled is False
    assert a.executor_email == "bob@x"
    assert a.max_obs_seconds == 3600.0


# -- engine gating ----------------------------------------------------------

def test_lockout_denies_control(tmp_path):
    eng = _engine(tmp_path, {AUTHORITY_KEY: {"agents_enabled": False}})
    d = eng.evaluate("fire_injection", {}, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.DENIED and d.reason == "agent control is locked out from the dashboard"


def test_executor_pin_blocks_other_user(tmp_path):
    eng = _engine(tmp_path, {AUTHORITY_KEY: {"agents_enabled": True,
                                             "executor_email": "carol@dsa110.org"}})
    # alice holds the lease but the dashboard pinned carol
    d = eng.evaluate("fire_injection", {}, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.DENIED and "pinned the executor" in d.reason


def test_executor_pin_allows_named_user(tmp_path):
    eng = _engine(tmp_path, {AUTHORITY_KEY: {"agents_enabled": True,
                                             "executor_email": ACTOR}})
    d = eng.evaluate("fire_injection", {}, actor=ACTOR, session_id=SID)
    assert d.outcome is Outcome.SHADOW


def test_lockout_cannot_be_cleared_by_operator_writer(tmp_path):
    # The operator's writer must refuse to touch the authority key at all.
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    with pytest.raises(ValueError):
        writer.put(AUTHORITY_KEY, {"agents_enabled": True})


# -- observation watchdog ---------------------------------------------------

def test_observation_status_not_armed():
    s = observation_status(ReadOnlyEtcd(FakeEtcdReader({})), 3600, now_unix=1e9)
    assert s.armed is False and s.overrun is False


def test_observation_overrun_detected():
    now = 1_700_000_000.0
    armed_mjd = astro.unix_to_mjd(now - 7200)        # armed 2h ago
    read = ReadOnlyEtcd(FakeEtcdReader({"/mon/snap/1/armed_mjd": {"armed_mjd": armed_mjd}}))
    s = observation_status(read, max_obs_seconds=3600, now_unix=now)
    assert s.armed is True
    assert s.elapsed_s == pytest.approx(7200, abs=1)
    assert s.overrun is True
    assert s.to_json()["remaining_s"] == pytest.approx(-3600, abs=1)


def test_observation_within_limit():
    now = 1_700_000_000.0
    armed_mjd = astro.unix_to_mjd(now - 600)         # 10 min ago
    read = ReadOnlyEtcd(FakeEtcdReader({"/mon/snap/1/armed_mjd": {"armed_mjd": armed_mjd}}))
    s = observation_status(read, max_obs_seconds=3600, now_unix=now)
    assert s.armed is True and s.overrun is False


def test_engine_observation_status_uses_authority_cap(tmp_path):
    now_unix = 1_700_000_000.0
    armed_mjd = astro.unix_to_mjd(now_unix - 5000)
    eng = _engine(tmp_path, {
        AUTHORITY_KEY: {"agents_enabled": True, "max_obs_seconds": 3600},
        "/mon/snap/1/armed_mjd": {"armed_mjd": armed_mjd}})
    eng._now = lambda: now_unix
    s = eng.observation_status()
    assert s.overrun is True
