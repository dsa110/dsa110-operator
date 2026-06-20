"""Read-only live shakeout of the operator stack against real h23 etcd +
dashboard. It only reads and runs a shadow control round-trip (no lease, so
it cannot mutate).

By default it talks to the SSH-forwarded ports (open the tunnel first:
``python -m dsa_operator.transport.ssh_tunnel --ssh-host h23``):

    python scripts/shakeout.py

To run it directly ON h23 (no tunnel), point it at the real endpoints:

    DSA_OPERATOR_ETCD_HOST=etcdv3service.pro.pvt DSA_OPERATOR_ETCD_PORT=2379 \
    DSA_OPERATOR_DASHBOARD_PORT=5778 \
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python \
    python scripts/shakeout.py
"""
from __future__ import annotations

import json
import os
import tempfile

from dsa_operator import DEFAULT_LOCAL_DASHBOARD_PORT, DEFAULT_LOCAL_ETCD_PORT
from dsa_operator.audit.log import AuditLog
from dsa_operator.control.approvals import ApprovalStore
from dsa_operator.control.engine import ControlEngine
from dsa_operator.control.lease import ExecutorLease
from dsa_operator.dashboard import DashboardClient
from dsa_operator.etcd.read import connect_readonly
from dsa_operator.etcd.write import FakeOperatorBackend, OperatorEtcdWriter
from dsa_operator.policy import load_policy
from dsa_operator.tools.readonly import ReadOnlyTools


def _show(label, fn):
    try:
        out = fn()
        s = json.dumps(out, default=str)
        print(f"  [ok]  {label}: {s[:240]}")
    except Exception as exc:                                   # noqa: BLE001
        print(f"  [ERR] {label}: {exc}")


def main() -> int:
    host = os.environ.get("DSA_OPERATOR_ETCD_HOST", "127.0.0.1")
    eport = int(os.environ.get("DSA_OPERATOR_ETCD_PORT", str(DEFAULT_LOCAL_ETCD_PORT)))
    dport = int(os.environ.get("DSA_OPERATOR_DASHBOARD_PORT",
                               str(DEFAULT_LOCAL_DASHBOARD_PORT)))
    print(f"== shakeout: etcd {host}:{eport}, dashboard 127.0.0.1:{dport} ==")

    read = connect_readonly(host=host, port=eport)
    dash = DashboardClient(port=dport)
    audit = AuditLog(tempfile.mkdtemp(prefix="shakeout-audit-"))
    tools = ReadOnlyTools(read, dash, audit, actor="shakeout")

    print("\n-- read-only tools (live) --")
    _show("get_fleet_status", tools.get_fleet_status)
    _show("get_array_pointing", tools.get_array_pointing)
    _show("get_sky_status", tools.get_sky_status)
    _show("get_sefd", tools.get_sefd)
    _show("get_rfi_summary", tools.get_rfi_summary)
    _show("query_injections", tools.query_injections)
    _show("get_audit_log", lambda: tools.get_audit_log(3))

    print("\n-- control engine over live etcd (read authority + gauntlet) --")
    # Write surface is a FAKE backend, so this CANNOT mutate production etcd;
    # only the READ path (authority, lease holder) hits real etcd.
    engine = ControlEngine(
        load_policy(), ExecutorLease(OperatorEtcdWriter(FakeOperatorBackend())),
        ApprovalStore(), audit, writer=OperatorEtcdWriter(FakeOperatorBackend()),
        read_etcd=read)
    _show("authority (/cmd/operator/control)", lambda: engine.authority().__dict__)
    _show("observation_status", lambda: engine.observation_status().to_json())
    # Shadow control round-trip: no lease held -> must be DENIED (proves the
    # gauntlet reads live state and refuses to act).
    d = engine.evaluate("point_array", {"dec_deg": 33.0},
                        actor="shakeout", session_id="nope")
    print(f"  [ok]  point_array w/o lease -> {d.outcome.value} ({d.reason})")
    assert d.outcome.value == "denied", "expected denial without lease"
    print("\n== shakeout complete ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
