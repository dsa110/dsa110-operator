"""SSH transport: the only way dsa110-operator reaches the observatory.

A single SSH connection to ``h23`` carries two local port-forwards — etcd
and the ``dsa_monitor`` dashboard — both reached *through* ``h23`` as the
jump host. No other host is ever contacted; there is no raw-shell tool.
"""
from __future__ import annotations

from dsa_operator.transport.ssh_tunnel import (
    Forward,
    SshTunnel,
    build_ssh_command,
    default_forwards,
)

__all__ = ["Forward", "SshTunnel", "build_ssh_command", "default_forwards"]
