"""Agent layer: stub routing, tool catalog, default selection."""
from __future__ import annotations

import pytest

from dsa_operator.agent import build_default_agent
from dsa_operator.agent.base import READONLY_TOOL_SPECS, TOOL_SPECS_BY_NAME
from dsa_operator.agent.context import SYSTEM_PROMPT, tool_schema_json
from dsa_operator.agent.stub import StubAgent, _route
from dsa_operator.audit.log import AuditLog
from dsa_operator.dashboard import DashboardClient
from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd
from dsa_operator.tools.readonly import ReadOnlyTools


def _tools(tmp_path):
    etcd = ReadOnlyEtcd(FakeEtcdReader({"/mon/array/dec": {"dec_deg": 33.0}}))

    def getter(url, timeout):
        return {"state": "ok"}

    dash = DashboardClient(getter=getter)
    return ReadOnlyTools(etcd, dash, AuditLog(tmp_path / "a"), actor="t")


@pytest.mark.parametrize("text,tool", [
    ("where is the array pointing?", "get_array_pointing"),
    ("any RFI right now?", "get_rfi_detail"),
    ("did the injection get detected?", "query_injections"),
    ("show me recent candidates", "list_candidates"),
    ("are all nodes up?", "get_fleet_status"),
    ("who changed things, audit?", "get_audit_log"),
    ("what is the sky image showing", "get_sky_status"),
    ("totally unrelated question", "get_fleet_status"),  # default
])
def test_route(text, tool):
    assert _route(text) == tool


def test_stub_chat_invokes_tool(tmp_path):
    agent = StubAgent()
    resp = agent.chat("where is the array pointing?", actor="t", tools=_tools(tmp_path))
    assert resp.model == "stub"
    assert resp.tool_calls[0].name == "get_array_pointing"
    assert "33.0" in resp.text


def test_tool_catalog_consistent():
    assert set(TOOL_SPECS_BY_NAME) == {s.name for s in READONLY_TOOL_SPECS}
    names = {s["name"] for s in tool_schema_json()}
    assert names == set(TOOL_SPECS_BY_NAME)
    # Every catalog tool must be a real read-only method.
    for name in TOOL_SPECS_BY_NAME:
        assert hasattr(ReadOnlyTools, name)


def test_system_prompt_is_read_only():
    assert "READ-ONLY" in SYSTEM_PROMPT


def test_build_default_agent_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(build_default_agent(), StubAgent)
