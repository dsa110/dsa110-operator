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
    (("point", "dec", "elevation", "where", "slew"), "get_array_pointing"),
    (("inject", "calibrat", "snr"), "query_injections"),
    (("rfi", "interference", "flag"), "get_rfi_summary"),
    (("sefd",), "get_sefd"),
    (("sky", "image", "continuum", "nvss"), "get_sky_status"),
    (("candidate", "burst", "event", "frb"), "list_candidates"),
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
    ) -> AgentResponse:
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


__all__ = ["StubAgent"]
