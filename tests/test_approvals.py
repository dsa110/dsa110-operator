"""Approval store: matching, TTL, single vs two-person rules."""
from __future__ import annotations

import pytest

from dsa_operator.control.approvals import ApprovalError, ApprovalStore


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


def test_single_approval_satisfies_matching_request():
    store = ApprovalStore()
    ap = store.request("point_array", {"dec_deg": 33.0}, requested_by="alice",
                       n_required=1, ttl_s=300)
    assert store.find_active("point_array", {"dec_deg": 33.0}) is None  # ungranted
    store.grant(ap.id, "alice")
    assert store.find_active("point_array", {"dec_deg": 33.0}) is not None


def test_param_mismatch_does_not_match():
    store = ApprovalStore()
    ap = store.request("point_array", {"dec_deg": 33.0}, requested_by="alice")
    store.grant(ap.id, "alice")
    assert store.find_active("point_array", {"dec_deg": 71.0}) is None


def test_expiry():
    clock = Clock()
    store = ApprovalStore(now=clock)
    ap = store.request("point_array", {"dec_deg": 33.0}, requested_by="alice",
                       ttl_s=300)
    store.grant(ap.id, "alice")
    assert store.find_active("point_array", {"dec_deg": 33.0}) is not None
    clock.tick(301)
    assert store.find_active("point_array", {"dec_deg": 33.0}) is None


def test_two_person_needs_two_distinct_non_requester():
    store = ApprovalStore()
    ap = store.request("set_policy", {"x": 1}, requested_by="alice",
                       n_required=2, two_person=True)
    with pytest.raises(ApprovalError):
        store.grant(ap.id, "alice")             # requester can't approve
    store.grant(ap.id, "bob")
    assert store.find_active("set_policy", {"x": 1}) is None  # only 1 so far
    with pytest.raises(ApprovalError):
        store.grant(ap.id, "bob")               # no double-grant
    store.grant(ap.id, "carol")
    assert store.find_active("set_policy", {"x": 1}) is not None


def test_consume_removes_grant():
    store = ApprovalStore()
    ap = store.request("dump_now", {}, requested_by="alice")
    store.grant(ap.id, "alice")
    assert store.find_active("dump_now", {}) is not None
    store.consume(ap.id)
    assert store.find_active("dump_now", {}) is None


def test_grant_unknown_raises():
    store = ApprovalStore()
    with pytest.raises(ApprovalError):
        store.grant("nope", "alice")
