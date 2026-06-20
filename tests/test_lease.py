"""Single-executor lease: mutual exclusion, refresh, expiry, takeover."""
from __future__ import annotations

from dsa_operator.control.lease import ExecutorLease
from dsa_operator.etcd.write import (
    OPERATOR_PREFIX,
    FakeOperatorBackend,
    OperatorEtcdWriter,
)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


def _lease(clock=None, ttl=30):
    clock = clock or Clock()
    backend = FakeOperatorBackend(now=clock)
    writer = OperatorEtcdWriter(backend)
    return ExecutorLease(writer, ttl_s=ttl, host="laptop", now=clock), clock


def test_single_holder_wins():
    lease, _ = _lease()
    assert lease.acquire("alice", "sid-a") is True
    # A second, independent lease object (another session) cannot take it.
    lease2 = ExecutorLease(lease._w, ttl_s=30, now=lease._now)
    assert lease2.acquire("bob", "sid-b") is False
    h = lease.holder()
    assert h.actor == "alice" and h.session_id == "sid-a"


def test_same_session_acquire_is_idempotent():
    lease, _ = _lease()
    assert lease.acquire("alice", "sid-a") is True
    assert lease.acquire("alice", "sid-a") is True
    assert lease.holder().actor == "alice"


def test_release_frees_the_lease():
    lease, _ = _lease()
    lease.acquire("alice", "sid-a")
    assert lease.release("sid-a") is True
    assert lease.holder() is None
    assert lease.acquire("bob", "sid-b") is True


def test_release_requires_matching_session():
    lease, _ = _lease()
    lease.acquire("alice", "sid-a")
    assert lease.release("sid-b") is False
    assert lease.holder().actor == "alice"


def test_lease_expires_when_not_refreshed():
    clock = Clock()
    lease, _ = _lease(clock=clock, ttl=30)
    lease.acquire("alice", "sid-a")
    clock.tick(31)
    assert lease.holder() is None          # expired
    lease2 = ExecutorLease(lease._w, ttl_s=30, now=clock)
    assert lease2.acquire("bob", "sid-b") is True


def test_refresh_keeps_the_lease():
    clock = Clock()
    lease, _ = _lease(clock=clock, ttl=30)
    lease.acquire("alice", "sid-a")
    clock.tick(20)
    lease.refresh()
    clock.tick(20)                          # 40s total, but refreshed at 20s
    assert lease.holder() is not None
    assert lease.holder().actor == "alice"


def test_keepalive_idle_when_no_lease():
    lease, _ = _lease()
    assert lease.keepalive() == "idle"


def test_keepalive_holds_while_refreshing():
    clock = Clock()
    lease, _ = _lease(clock=clock, ttl=30)
    lease.acquire("alice", "sid-a")
    clock.tick(20)
    assert lease.keepalive() == "held"     # refreshes
    clock.tick(20)                         # 40s total, refreshed at 20s
    assert lease.holder().actor == "alice"


def test_keepalive_reports_lost_after_lapse():
    """Laptop sleeps: TTL passes with no keepalive; on wake we report lost."""
    clock = Clock()
    lease, _ = _lease(clock=clock, ttl=30)
    lease.acquire("alice", "sid-a")
    clock.tick(31)                         # slept past the TTL
    assert lease.holder() is None          # key gone on the server
    assert lease.keepalive() == "lost"     # local state detects + clears it
    assert lease.keepalive() == "idle"     # cleared, nothing to do now


def test_keepalive_reports_lost_on_takeover():
    clock = Clock()
    lease, _ = _lease(clock=clock, ttl=30)
    lease.acquire("alice", "sid-a")
    taker = ExecutorLease(lease._w, ttl_s=30, now=clock)
    taker.takeover("bob", "sid-b")
    assert lease.keepalive() == "lost"     # alice notices bob took over


def test_takeover_seizes_from_incumbent():
    lease, _ = _lease()
    lease.acquire("alice", "sid-a")
    taker = ExecutorLease(lease._w, ttl_s=30, now=lease._now)
    assert taker.takeover("bob", "sid-b") is True
    assert lease.holder().actor == "bob"


def test_writer_refuses_non_operator_keys():
    writer = OperatorEtcdWriter(FakeOperatorBackend())
    import pytest

    with pytest.raises(ValueError):
        writer.put("/cmd/ant/1", {"cmd": "move", "val": 70})
    # operator-namespaced writes are fine
    writer.put(OPERATOR_PREFIX + "x", {"ok": True})
