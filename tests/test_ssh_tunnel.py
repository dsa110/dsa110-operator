"""Pure-construction tests for the SSH tunnel argv (no real connection)."""
import pytest

from dsa_operator.transport.ssh_tunnel import (
    Forward,
    SshTunnel,
    _main,
    build_ssh_command,
    default_forwards,
)


def test_default_forwards_are_etcd_and_dashboard():
    fwds = default_forwards()
    labels = {f.label for f in fwds}
    assert labels == {"etcd", "dashboard"}
    etcd = next(f for f in fwds if f.label == "etcd")
    assert etcd.remote_host == "etcdv3service.pro.pvt"
    assert etcd.remote_port == 2379
    dash = next(f for f in fwds if f.label == "dashboard")
    assert dash.remote_host == "localhost"
    assert dash.remote_port == 5778


def test_forward_binds_loopback_only():
    f = Forward(12379, "etcdv3service.pro.pvt", 2379)
    assert f.as_l_arg() == "127.0.0.1:12379:etcdv3service.pro.pvt:2379"


def test_build_ssh_command_no_remote_shell_and_failclosed():
    cmd = build_ssh_command("h23", default_forwards())
    assert cmd[0] == "ssh"
    assert "-N" in cmd                       # no remote command / shell
    assert "ExitOnForwardFailure=yes" in cmd
    assert "BatchMode=yes" in cmd            # never prompt
    # ServerAlive* makes ssh exit promptly after a laptop suspends, so the
    # supervised reconnect loop can bring the tunnel straight back up.
    assert "ServerAliveInterval=15" in cmd
    assert cmd[-1] == "h23"                  # host is last positional
    # exactly two -L forwards
    assert cmd.count("-L") == 2


def test_build_ssh_command_rejects_no_forwards():
    with pytest.raises(ValueError):
        build_ssh_command("h23", [])


def test_tunnel_requires_host():
    with pytest.raises(ValueError):
        SshTunnel(ssh_host="")


def test_tunnel_command_property_matches_builder():
    tun = SshTunnel(ssh_host="myh23")
    assert tun.command == build_ssh_command("myh23", default_forwards())


def test_main_print_cmd_does_not_connect(capsys):
    rc = _main(["--ssh-host", "h23", "--print-cmd"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("ssh ") and "h23" in out
