"""The real Claude brain (Anthropic API via the Claude Agent SDK).

Imports the SDK lazily so the package works without it. Registers ONLY the
read-only tools; the model is given the read-only system prompt and runs a
short tool-use loop. Each tool result is the compact summary the tool
already produces (no raw telemetry), and every call is audited by the
ReadOnlyTools layer itself.

Verified live only on a machine with ``ANTHROPIC_API_KEY`` set and
``claude-agent-sdk`` installed (the operator's laptop). On h23/CI without
those, :func:`dsa_operator.agent.build_default_agent` selects the stub
instead.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from dsa_operator.agent.base import TOOL_SPECS_BY_NAME, AgentResponse, ToolCall
from dsa_operator.agent.context import system_prompt, tool_schema_json
from dsa_operator.agent.control import CONTROL_SPECS_BY_NAME
from dsa_operator.tools.readonly import ReadOnlyTools

LOG = logging.getLogger("dsa_operator.agent.claude")

DEFAULT_MODEL = os.environ.get("DSA_OPERATOR_MODEL", "claude-sonnet-4-5")
MAX_TOOL_ITERS = 6


class ClaudeAgent:
    """Anthropic tool-use loop over the read-only tools.

    Uses the ``anthropic`` Messages API (shipped as a dependency of the
    Claude Agent SDK). Kept deliberately small and synchronous to match
    the Flask request model.
    """

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        import anthropic  # lazy; provided by claude-agent-sdk

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic()
        self.model = model

    def chat(
        self, message: str, *, actor: str, tools: ReadOnlyTools,
        control: Any = None,
    ) -> AgentResponse:
        messages: list[dict[str, Any]] = [{"role": "user", "content": message}]
        calls: list[ToolCall] = []
        control_enabled = control is not None
        tool_schemas = tool_schema_json(include_control=control_enabled)
        prompt = system_prompt(control_enabled=control_enabled)

        for _ in range(MAX_TOOL_ITERS):
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=prompt,
                tools=tool_schemas,
                messages=messages,
            )
            if resp.stop_reason != "tool_use":
                text = "".join(
                    b.text for b in resp.content if getattr(b, "type", "") == "text"
                )
                return AgentResponse(text=text, tool_calls=calls, model=self.model)

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                call, payload = self._run_tool(block, tools, control)
                calls.append(call)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(payload, default=str)[:6000],
                    "is_error": not call.ok,
                })
            messages.append({"role": "user", "content": tool_results})

        return AgentResponse(
            text="(reached tool-iteration limit without a final answer)",
            tool_calls=calls, model=self.model,
        )

    def _run_tool(self, block: Any, tools: ReadOnlyTools,
                  control: Any) -> tuple[ToolCall, Any]:
        name = block.name
        args = dict(block.input or {})
        call = ToolCall(name=name, args=args)
        spec = TOOL_SPECS_BY_NAME.get(name)
        if spec is not None:
            target, invoke = tools, spec.invoke
        elif control is not None and name in CONTROL_SPECS_BY_NAME:
            cspec = CONTROL_SPECS_BY_NAME[name]
            target, invoke = control, cspec.invoke
        else:
            call.ok = False
            call.error = f"unknown tool {name}"
            return call, {"error": call.error}
        try:
            return call, invoke(target, args)
        except Exception as exc:                           # noqa: BLE001
            call.ok = False
            call.error = str(exc)
            return call, {"error": str(exc)}


__all__ = ["ClaudeAgent"]
