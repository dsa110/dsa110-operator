"""The agent layer (the Claude "brain").

The web console talks to an :class:`~dsa_operator.agent.base.Agent`. Two
implementations ship:

* :class:`~dsa_operator.agent.claude.ClaudeAgent` — the real brain, via the
  ``anthropic`` Messages API client on the operator's own Anthropic account
  (the ``agent`` extra: ``pip install .[agent]``).
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

import logging

from dsa_operator.agent.base import Agent, AgentResponse, ToolCall
from dsa_operator.agent.stub import StubAgent

LOG = logging.getLogger("dsa_operator.agent")

__all__ = ["Agent", "AgentResponse", "ToolCall", "StubAgent", "build_default_agent"]


def build_default_agent() -> Agent:
    """Return :class:`ClaudeAgent` if the ``anthropic`` package + API key are
    available, else the deterministic :class:`StubAgent`.

    Loads secrets first (env or a git-ignored local file), so the single
    Anthropic key the server holds is picked up without ever being in git.

    Any reason the real brain can't be built is logged at WARNING (the names
    only, never the key value) so a silent fall-back to the stub is
    diagnosable — e.g. "No module named 'anthropic'" means the ``agent``
    extra isn't installed (``pip install .[agent]``).
    """
    from dsa_operator.env import have_anthropic_key, load_secrets

    load_secrets()
    if not have_anthropic_key():
        LOG.warning("ANTHROPIC_API_KEY not found in env or secrets files; "
                    "using the stub agent (monitoring + Q&A still work)")
        return StubAgent()
    try:
        from dsa_operator.agent.claude import ClaudeAgent

        return ClaudeAgent()
    except Exception as exc:                               # noqa: BLE001
        LOG.warning("ANTHROPIC_API_KEY is set but the real Claude agent could "
                    "not be built (%s: %s); falling back to the stub. Install "
                    "the agent extra with `pip install .[agent]`.",
                    type(exc).__name__, exc)
        return StubAgent()
