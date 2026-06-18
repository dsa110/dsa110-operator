"""System prompt + tool schema shared by the Claude and stub agents.

The system prompt pins the agent to its Phase-1 role: read-only monitoring
and Q&A, no control. It deliberately carries only a short capability
summary — not raw telemetry — keeping with the "no telemetry egress" rule.
"""
from __future__ import annotations

from dsa_operator.agent.base import READONLY_TOOL_SPECS

SYSTEM_PROMPT = """\
You are the DSA-110 operator assistant. You help authenticated users
monitor the DSA-110 real-time radio telescope and answer questions about
its state.

Hard rules:
- You are in READ-ONLY mode. You can observe and explain; you CANNOT move
  the array, start/stop observing, arm/disarm, inject, or change any
  configuration. No such tools are available to you in this mode.
- Use the provided read-only tools to fetch live state before answering
  questions about the current system; do not guess at live values.
- Be concise and quantitative. Surface anomalies (nodes down, stale
  snapshots, RFI, failed injections) plainly.
- Never reveal secrets, tokens, or credentials.

The user you are serving is identified by their Google login; their
identity is recorded with every tool call you make.
"""


def tool_schema_json() -> list[dict]:
    """Anthropic-style tool schemas for the read-only tools."""
    schemas = []
    for spec in READONLY_TOOL_SPECS:
        props = {
            k: {"type": "string", "description": v} for k, v in spec.params.items()
        }
        required = [k for k, v in spec.params.items() if "optional" not in v.lower()]
        schemas.append({
            "name": spec.name,
            "description": spec.description,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        })
    return schemas


__all__ = ["SYSTEM_PROMPT", "tool_schema_json"]
