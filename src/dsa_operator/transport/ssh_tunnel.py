"""SSH tunnel manager — forward etcd + dashboard through ``h23``.

The operator laptop reaches the observatory over exactly one SSH hop to
``h23``. etcd lives on ``etcdv3service.pro.pvt:2379`` (reachable *from*
``h23``) and the ``dsa_monitor`` dashboard runs on ``h23`` itself, so we
open two ``-L`` forwards on a single connection::

    localhost:12379  ->  etcdv3service.pro.pvt:2379   (via h23)
    localhost:15778  ->  localhost:5778               (on h23)

Security notes
==============

* We never run an interactive shell or arbitrary remote command here.
  This module only sets up port-forwards. The remote ``authorized_keys``
  entry for the operator key should be locked down with
  ``restrict,permitopen="etcdv3service.pro.pvt:2379",permitopen="localhost:5778",command=""``
  so the key *cannot* do anything but open these two forwards.
* ``ExitOnForwardFailure=yes`` makes the process fail fast (non-zero
  exit) if either forward can't be established, rather than silently
  giving the agent a dead etcd port.
* Host-key checking is left at the SSH default (``accept-new`` is
  recommended in the operator's ``~/.ssh/config``); we never disable it.

The class is intentionally a thin, testable wrapper: :func:`build_ssh_command`
is pure (no I/O) so the exact argv can be unit-tested, and :class:`SshTunnel`
manages the subprocess lifecycle.
"""
from __future__ import annotations

import logging
import socket
import subprocess
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Optional, Sequence

from dsa_operator import (
    DEFAULT_LOCAL_DASHBOARD_PORT,
    DEFAULT_LOCAL_ETCD_PORT,
    H23_DASHBOARD_HOST,
    H23_DASHBOARD_PORT,
    H23_ETCD_HOST,
    H23_ETCD_PORT,
)

LOG = logging.getLogger("dsa_operator.transport")


@dataclass(frozen=True)
class Forward:
    """One ``ssh -L`` local port-forward.

    ``local_port`` on the laptop's loopback is forwarded to
    ``remote_host:remote_port`` as resolved *from the SSH server* (h23).
    """

    local_port: int
    remote_host: str
    remote_port: int
    label: str = ""

    def as_l_arg(self) -> str:
        # Bind to loopback only — never expose the forward on the LAN.
        return f"127.0.0.1:{self.local_port}:{self.remote_host}:{self.remote_port}"


def default_forwards(
    *,
    local_etcd_port: int = DEFAULT_LOCAL_ETCD_PORT,
    local_dashboard_port: int = DEFAULT_LOCAL_DASHBOARD_PORT,
) -> list[Forward]:
    """The two forwards Phase 0 needs: etcd and the dashboard."""
    return [
        Forward(local_etcd_port, H23_ETCD_HOST, H23_ETCD_PORT, "etcd"),
        Forward(
            local_dashboard_port, H23_DASHBOARD_HOST, H23_DASHBOARD_PORT,
            "dashboard",
        ),
    ]


def build_ssh_command(
    ssh_host: str,
    forwards: Sequence[Forward],
    *,
    ssh_binary: str = "ssh",
    extra_opts: Optional[Sequence[str]] = None,
) -> list[str]:
    """Construct the (pure) ssh argv for the forwarding connection.

    No remote command is run (``-N``); the connection exists only to
    carry the ``-L`` forwards. ``ExitOnForwardFailure`` ensures we don't
    end up with a live SSH session but a dead forward.
    """
    if not forwards:
        raise ValueError("refusing to open an SSH tunnel with no forwards")
    cmd = [
        ssh_binary,
        "-N",                                   # no remote command / shell
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "BatchMode=yes",                  # never prompt; fail instead
    ]
    for fwd in forwards:
        cmd += ["-L", fwd.as_l_arg()]
    if extra_opts:
        cmd += list(extra_opts)
    cmd.append(ssh_host)
    return cmd


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class SshTunnel:
    """Manage the lifetime of the forwarding SSH process.

    Usage::

        with SshTunnel(ssh_host="h23") as tun:
            tun.wait_ready()
            ...  # localhost:12379 (etcd) and :15778 (dashboard) are live
    """

    def __init__(
        self,
        ssh_host: str = "h23",
        forwards: Optional[Sequence[Forward]] = None,
        *,
        ssh_binary: str = "ssh",
        extra_opts: Optional[Sequence[str]] = None,
    ) -> None:
        if not ssh_host:
            raise ValueError("ssh_host is required (e.g. 'h23')")
        self.ssh_host = ssh_host
        self.forwards = list(forwards) if forwards is not None else default_forwards()
        self.ssh_binary = ssh_binary
        self.extra_opts = list(extra_opts) if extra_opts else []
        self._proc: Optional[subprocess.Popen] = None

    @property
    def command(self) -> list[str]:
        return build_ssh_command(
            self.ssh_host, self.forwards,
            ssh_binary=self.ssh_binary, extra_opts=self.extra_opts,
        )

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        cmd = self.command
        LOG.info("opening ssh tunnel: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def wait_ready(self, timeout: float = 15.0) -> bool:
        """Block until all forwarded local ports accept connections.

        Returns True once every forward's local port is open; False on
        timeout or if the ssh process exited early.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                rc = None if self._proc is None else self._proc.returncode
                LOG.error("ssh tunnel exited early (rc=%s)", rc)
                return False
            if all(_port_open(f.local_port) for f in self.forwards):
                LOG.info("ssh tunnel ready (%d forwards)", len(self.forwards))
                return True
            time.sleep(0.25)
        LOG.error("ssh tunnel not ready after %.1fs", timeout)
        return False

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        LOG.info("ssh tunnel closed")

    def __enter__(self) -> "SshTunnel":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.stop()


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Open the h23 SSH tunnel (etcd + dashboard).")
    p.add_argument("--ssh-host", default="h23",
                   help="SSH host alias for h23 (default: h23)")
    p.add_argument("--etcd-port", type=int, default=DEFAULT_LOCAL_ETCD_PORT)
    p.add_argument("--dashboard-port", type=int, default=DEFAULT_LOCAL_DASHBOARD_PORT)
    p.add_argument("--print-cmd", action="store_true",
                   help="print the ssh command and exit (no connection)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fwds = default_forwards(
        local_etcd_port=args.etcd_port, local_dashboard_port=args.dashboard_port,
    )
    if args.print_cmd:
        print(" ".join(build_ssh_command(args.ssh_host, fwds)))
        return 0

    tun = SshTunnel(ssh_host=args.ssh_host, forwards=fwds)
    tun.start()
    if not tun.wait_ready():
        tun.stop()
        return 1
    print(f"tunnel up: etcd -> 127.0.0.1:{args.etcd_port}, "
          f"dashboard -> 127.0.0.1:{args.dashboard_port}. Ctrl-C to close.")
    try:
        while tun.is_alive():
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        tun.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
