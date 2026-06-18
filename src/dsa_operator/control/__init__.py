"""Control plane (Phase 2): single-executor lease, policy gate engine,
approvals, and typed control verbs.

Everything here is **shadow-only** in this build: the :class:`ControlEngine`
has no live executor, so the strongest outcome of any control verb is a
fully-rendered *plan* of the etcd puts / dashboard POSTs it WOULD make,
audited as a dry run. Live execution is wired in Phase 3, behind the same
lease + gate + approval checks proven here.
"""
from __future__ import annotations

from dsa_operator.control.approvals import Approval, ApprovalStore
from dsa_operator.control.authority import (
    Authority,
    ObservationStatus,
    observation_status,
    read_authority,
)
from dsa_operator.control.engine import ControlEngine, Decision, Outcome
from dsa_operator.control.executors import (
    ControlEtcdWriter,
    DashboardControlClient,
    ExecutorError,
    LiveExecutor,
)
from dsa_operator.control.lease import ExecutorLease, LeaseHolder

__all__ = [
    "ExecutorLease",
    "LeaseHolder",
    "ApprovalStore",
    "Approval",
    "ControlEngine",
    "Decision",
    "Outcome",
    "LiveExecutor",
    "ControlEtcdWriter",
    "DashboardControlClient",
    "ExecutorError",
    "Authority",
    "read_authority",
    "ObservationStatus",
    "observation_status",
]
