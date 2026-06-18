"""dsa110-operator — agent-driven control + monitoring for DSA-110.

Phase 0 exposes only the read-only foundation: the SSH tunnel manager
(:mod:`dsa_operator.transport`), the read-only etcd client
(:mod:`dsa_operator.etcd`), the typed read-only tool surface
(:mod:`dsa_operator.tools`), and the audit log
(:mod:`dsa_operator.audit`). No control/mutating surface exists yet.
"""
from __future__ import annotations

__version__ = "0.0.1"

# Forwarded local ports the tunnel manager opens on the laptop. Kept here
# so the etcd client and dashboard client agree with the tunnel without a
# circular import.
DEFAULT_LOCAL_ETCD_PORT = 12379
DEFAULT_LOCAL_DASHBOARD_PORT = 15778

# Where etcd and the dashboard actually live, as seen FROM h23. The tunnel
# forwards localhost:<above> -> these, with h23 as the jump host.
H23_ETCD_HOST = "etcdv3service.pro.pvt"
H23_ETCD_PORT = 2379
H23_DASHBOARD_HOST = "localhost"        # dsa_monitor runs on h23 itself
H23_DASHBOARD_PORT = 5778

__all__ = [
    "__version__",
    "DEFAULT_LOCAL_ETCD_PORT",
    "DEFAULT_LOCAL_DASHBOARD_PORT",
    "H23_ETCD_HOST",
    "H23_ETCD_PORT",
    "H23_DASHBOARD_HOST",
    "H23_DASHBOARD_PORT",
]
