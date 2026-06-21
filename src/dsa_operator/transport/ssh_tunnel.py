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
import signal
import socket
import subprocess
import threading
import time
from collections import deque
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
        # Keep the last few lines ssh wrote so we can show *why* it exited
        # (e.g. "bind: Address already in use") instead of a bare rc=255.
        self._output: "deque[str]" = deque(maxlen=50)
        self._reader: Optional[threading.Thread] = None

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
        self._output.clear()
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Drain ssh's output on a daemon thread so the pipe never fills and we
        # always have its diagnostics handy if it dies.
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self._output.append(line)
                    LOG.debug("ssh: %s", line)
        except Exception:                                  # noqa: BLE001
            pass

    def recent_output(self) -> list[str]:
        """The last lines ssh emitted (most useful right after it exits)."""
        return list(self._output)

    def wait_ready(self, timeout: float = 15.0) -> bool:
        """Block until all forwarded local ports accept connections.

        Returns True once every forward's local port is open; False on
        timeout or if the ssh process exited early.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                rc = None if self._proc is None else self._proc.returncode
                # Give the reader a beat to flush ssh's final lines, then show
                # them — this is where "Address already in use" appears.
                if self._reader is not None:
                    self._reader.join(timeout=1.0)
                LOG.error("ssh tunnel exited early (rc=%s)", rc)
                for line in self.recent_output():
                    LOG.error("  ssh: %s", line)
                if rc == 255 and any("address already in use" in s.lower()
                                     or "cannot listen to port" in s.lower()
                                     for s in self.recent_output()):
                    LOG.error("  -> a local forward port is already taken; "
                              "another tunnel is probably already running.")
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
    p.add_argument("--no-retry", action="store_true",
                   help="exit when the tunnel drops instead of reconnecting "
                        "(default: supervise + reconnect, e.g. after laptop sleep)")
    p.add_argument("--max-backoff", type=float, default=30.0,
                   help="cap on the reconnect backoff in seconds (default 30)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fwds = default_forwards(
        local_etcd_port=args.etcd_port, local_dashboard_port=args.dashboard_port,
    )
    if args.print_cmd:
        print(" ".join(build_ssh_command(args.ssh_host, fwds)))
        return 0

    # laptop.sh runs us in the background and stops us with `kill` (SIGTERM) on
    # Ctrl-C. Python's default SIGTERM action exits *without* running finally
    # blocks, which would orphan the ssh child and leak the forwarded port
    # (e.g. 12379). Turn SIGTERM into KeyboardInterrupt so the cleanup below
    # always runs and the ssh child is reaped.
    def _on_term(signum, _frame):                          # pragma: no cover
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_term)

    def _serve_once() -> bool:
        """Bring the tunnel up and block until it drops. Returns True if it was
        ever ready (so the supervisor can reset its backoff)."""
        tun = SshTunnel(ssh_host=args.ssh_host, forwards=fwds)
        tun.start()
        # Always stop the tunnel (terminating the ssh child, which holds the
        # forwarded ports) on every exit path — including KeyboardInterrupt
        # from Ctrl-C or a SIGTERM from laptop.sh.
        try:
            if not tun.wait_ready():
                return False
            print(f"tunnel up: etcd -> 127.0.0.1:{args.etcd_port}, "
                  f"dashboard -> 127.0.0.1:{args.dashboard_port}.")
            while tun.is_alive():
                time.sleep(1.0)
            LOG.warning("ssh tunnel dropped")
            return True
        finally:
            tun.stop()

    if args.no_retry:
        try:
            return 0 if _serve_once() else 1
        except KeyboardInterrupt:
            print("\nclosing tunnel.")
            return 0

    # Supervised reconnect loop: ServerAliveInterval makes ssh exit a few
    # seconds after the laptop suspends or the link dies; we just bring it back
    # up. Exponential backoff (reset on a healthy run) avoids hammering h23.
    backoff = 1.0
    try:
        while True:
            ok = _serve_once()
            backoff = 1.0 if ok else min(args.max_backoff, backoff * 2)
            LOG.info("reconnecting in %.0fs…", backoff)
            time.sleep(backoff)
    except KeyboardInterrupt:
        print("\nclosing tunnel.")
        return 0


if __name__ == "__main__":
    raise SystemExit(_main())
