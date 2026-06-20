"""Deterministic, no-network agent.

Routes a question to the most relevant read-only tool by keyword and
formats a short answer. This is the dev/CI fallback (and what runs until
the operator drops in their Anthropic key), so the console's monitoring +
Q&A is always functional without the model.
"""
from __future__ import annotations

import json
from typing import Any

from dsa_operator.agent.base import AgentResponse, ToolCall
from dsa_operator.tools.readonly import ReadOnlyTools, ToolError

# (keywords) -> tool name. First match wins; order matters.
_ROUTES: list[tuple[tuple[str, ...], str]] = [
    (("what can you monitor", "what can i ask", "capabilit"), "describe_monitoring"),
    (("report card", "overall", "how is the telescope", "how's the telescope",
      "summary of", "everything ok"), "health_report"),
    (("capture", "kernel drop", "packet", "gbps", "data rate"), "get_capture_health"),
    (("buffer", "dada", "ring"), "get_buffer_health"),
    (("warm", "ready to arm", "prepared"), "get_warmup_status"),
    (("point", "dec", "elevation", "where", "slew"), "get_array_pointing"),
    (("inject", "calibrat", "snr", "k-factor", "kfactor"), "query_injections"),
    (("rfi", "interference", "flag"), "get_rfi_detail"),
    (("sefd", "sensitivity"), "get_sefd"),
    (("sky", "image", "continuum", "nvss"), "get_sky_status"),
    (("candidate", "burst", "event", "frb", "detection"), "list_candidates"),
    (("search", "noise", "clamp", "cube dump"), "get_search_health"),
    (("c2", "coincidenc", "trigger"), "get_c2_status"),
    (("service", "systemd", "failed"), "get_services_status"),
    (("dump",), "get_dumps_state"),
    (("spectral", "line mode"), "get_spectral_line_state"),
    (("voltage", "retention"), "get_voltage_retention"),
    (("audit", "who did", "history", "log"), "get_audit_log"),
    (("fleet", "node", "status", "up", "down", "running", "health",
      "observ"), "get_fleet_status"),
]


def _route(message: str) -> str:
    m = message.lower()
    for keywords, tool in _ROUTES:
        if any(k in m for k in keywords):
            return tool
    return "get_fleet_status"


class StubAgent:
    model = "stub"

    def chat(
        self, message: str, *, actor: str, tools: ReadOnlyTools,
        control: Any = None,
    ) -> AgentResponse:
        # Phase 6: when a control surface is present, route obvious control
        # intents deterministically. The stub never *executes* anything risky
        # on its own initiative — it reports lease/capabilities so a human can
        # drive; the real planning is the Claude brain's job.
        if control is not None:
            m = message.lower()
            if "lease" in m or "in charge" in m or "who controls" in m:
                return self._control_call(control, "lease_status", message)
            if "what can you" in m and ("control" in m or "do" in m or "run" in m):
                return self._control_call(control, "list_control_actions", message)

        tool_name = _route(message)
        call = ToolCall(name=tool_name)
        try:
            result: Any = getattr(tools, tool_name)()
        except ToolError as exc:
            call.ok = False
            call.error = str(exc)
            return AgentResponse(
                text=f"(stub) couldn't run {tool_name}: {exc}",
                tool_calls=[call], model=self.model,
            )
        text = (
            f"(stub agent — no LLM configured) For \"{message.strip()}\" I "
            f"queried `{tool_name}`:\n\n```json\n"
            f"{json.dumps(result, indent=2, default=str)[:1500]}\n```"
        )
        return AgentResponse(text=text, tool_calls=[call], model=self.model)

    def _control_call(self, control: Any, name: str, message: str) -> AgentResponse:
        from dsa_operator.agent.control import CONTROL_SPECS_BY_NAME
        call = ToolCall(name=name)
        try:
            result = CONTROL_SPECS_BY_NAME[name].invoke(control, {})
        except Exception as exc:                               # noqa: BLE001
            call.ok = False
            call.error = str(exc)
            return AgentResponse(text=f"(stub) couldn't run {name}: {exc}",
                                 tool_calls=[call], model=self.model)
        text = (f"(stub agent — no LLM configured) `{name}`:\n\n```json\n"
                f"{json.dumps(result, indent=2, default=str)[:1500]}\n```")
        return AgentResponse(text=text, tool_calls=[call], model=self.model)


__all__ = ["StubAgent"]
