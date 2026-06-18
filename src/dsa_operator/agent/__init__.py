"""The agent layer (the Claude "brain").

The web console talks to an :class:`~dsa_operator.agent.base.Agent`. Two
implementations ship:

* :class:`~dsa_operator.agent.claude.ClaudeAgent` — the real brain, via the
  Claude Agent SDK on the operator's own Anthropic account.
* :class:`~dsa_operator.agent.stub.StubAgent` — a deterministic,
  no-network fallback that routes a question to the matching read-only
  tool. Used in dev/CI and whenever the SDK or API key is absent, so
  monitoring + Q&A always work.

The agent always has the **read-only** tool surface. From Phase 6 the web
chat also hands it an :class:`~dsa_operator.agent.control.AgentControl`
surface, so it can *propose and run* control actions and drive the
observing plan — but every such call funnels through the same
``ControlEngine`` gauntlet (lease, dashboard lockout, e-stop, gate,
approval, shadow/live), so the agent can never exceed the policy a human
could enforce in the console.
"""
from __future__ import annotations

from dsa_operator.agent.base import Agent, AgentResponse, ToolCall
from dsa_operator.agent.stub import StubAgent

__all__ = ["Agent", "AgentResponse", "ToolCall", "StubAgent", "build_default_agent"]


def build_default_agent() -> Agent:
    """Return :class:`ClaudeAgent` if the SDK + API key are available,
    else the deterministic :class:`StubAgent`.

    Loads secrets first (env or a git-ignored local file), so the single
    Anthropic key the server holds is picked up without ever being in git.
    """
    from dsa_operator.env import have_anthropic_key, load_secrets

    load_secrets()
    if have_anthropic_key():
        try:
            from dsa_operator.agent.claude import ClaudeAgent

            return ClaudeAgent()
        except Exception:                                  # noqa: BLE001
            pass
    return StubAgent()
