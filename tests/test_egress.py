"""Egress allowlist: host checks + the opt-in socket tripwire."""
from __future__ import annotations

import socket

import pytest

from dsa_operator.audit import egress

_ALLOW = {"allow": {
    "anthropic": {"hosts": ["api.anthropic.com"]},
    "slack": {"hosts": ["hooks.slack.com"]},
    "h23_ssh": {"hosts": ["h23"], "transport": "ssh"},
}}


def test_allowed_hosts_excludes_ssh():
    hosts = egress.allowed_hosts(_ALLOW)
    assert "api.anthropic.com" in hosts and "hooks.slack.com" in hosts
    assert "h23" not in hosts          # ssh transport, not a direct socket


def test_host_allowed_rules():
    assert egress.host_allowed("api.anthropic.com", _ALLOW)
    assert egress.host_allowed("127.0.0.1", _ALLOW)        # loopback (tunnel)
    assert egress.host_allowed("localhost", _ALLOW)
    assert egress.host_allowed("10.41.0.94", _ALLOW)       # private
    assert not egress.host_allowed("evil.example.com", _ALLOW)
    assert not egress.host_allowed("", _ALLOW)


def test_assert_url_allowed():
    egress.assert_url_allowed("https://hooks.slack.com/services/x", _ALLOW)
    with pytest.raises(egress.EgressError):
        egress.assert_url_allowed("https://evil.example.com/x", _ALLOW)


def test_real_allowlist_loads():
    hosts = egress.allowed_hosts()        # the shipped config file
    assert "api.anthropic.com" in hosts
    assert "hooks.slack.com" in hosts


def test_slack_reads_url_env(monkeypatch):
    from dsa_operator.audit.slack import SlackNotifier
    monkeypatch.delenv("DSA_OPERATOR_SLACK_WEBHOOK", raising=False)
    monkeypatch.setenv("DSA_OPERATOR_SLACK_WEBHOOK_URL",
                       "https://hooks.slack.com/services/T/B/x")
    assert SlackNotifier().enabled


def test_socket_guard_blocks_and_restores():
    try:
        assert egress.install_socket_guard(_ALLOW)
        # loopback still resolves (no network)
        egress.socket.getaddrinfo("127.0.0.1", 80)
        with pytest.raises(egress.EgressError):
            socket.getaddrinfo("evil.example.com", 443)
    finally:
        egress.uninstall_socket_guard()
    # restored: the wrapper no longer raises for a denied host (it will try
    # real DNS, which we don't want in tests) — just assert it's the builtin.
    assert socket.getaddrinfo.__name__ == "getaddrinfo"
