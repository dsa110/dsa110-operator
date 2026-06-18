"""Agent interface + the read-only tool registry it is allowed to use."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from dsa_operator.tools.readonly import ReadOnlyTools


@dataclass
class ToolCall:
    """A single tool the agent invoked while answering."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    error: str = ""


@dataclass
class AgentResponse:
    """What the agent returns to a chat turn."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""


class Agent(Protocol):
    def chat(
        self, message: str, *, actor: str, tools: ReadOnlyTools,
        control: Any = None,
    ) -> AgentResponse:
        """Answer ``message`` on behalf of ``actor`` using ``tools``.

        ``control`` is an optional ``AgentControl`` surface (Phase 6); when
        provided the agent may also propose/run gated control actions.
        """
        ...


# --- the read-only tool catalog the agent may call -------------------------

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # (tools, kwargs) -> result; kwargs come from the model's tool call.
    invoke: Callable[[ReadOnlyTools, dict[str, Any]], Any]
    params: dict[str, str] = field(default_factory=dict)


READONLY_TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("get_fleet_status", "Roll-up of corr/search orchestrator "
             "heartbeats and the dashboard system-state banner.",
             lambda t, a: t.get_fleet_status()),
    ToolSpec("get_array_pointing", "Commanded array pointing: target dec "
             "and mean commanded elevation, plus how many antennas have "
             "not settled.",
             lambda t, a: t.get_array_pointing()),
    ToolSpec("get_mon", "Read one /mon/... etcd key (must start with /mon/).",
             lambda t, a: t.get_mon(a["key"]), {"key": "etcd key under /mon/"}),
    ToolSpec("get_audit_log", "Recent operator + control audit rows.",
             lambda t, a: t.get_audit_log(int(a.get("n", 50))),
             {"n": "how many rows (default 50)"}),
    ToolSpec("list_candidates", "Recent C2 burst-candidate directories.",
             lambda t, a: t.list_candidates()),
    ToolSpec("get_candidate", "One candidate's summary by name.",
             lambda t, a: t.get_candidate(a["name"]), {"name": "candidate name"}),
    ToolSpec("get_sefd", "SEFD scanner freshness / status.",
             lambda t, a: t.get_sefd()),
    ToolSpec("get_rfi_summary", "Per-chgroup RFI ring health.",
             lambda t, a: t.get_rfi_summary()),
    ToolSpec("get_sky_status", "Static-sky monitor frame + per-chgroup "
             "snapshot freshness.",
             lambda t, a: t.get_sky_status()),
    ToolSpec("query_injections", "Active injections, recent matches, and "
             "the C2 snapshot.",
             lambda t, a: t.query_injections()),
    ToolSpec("get_observing_plan", "The active observing plan (a timed "
             "schedule of declinations) and which segment is active now.",
             lambda t, a: t.get_observing_plan()),
    ToolSpec("get_observability", "For a declination (and optional RA): the "
             "transit elevation, whether it's within the pointing envelope, "
             "and the next transit time.",
             lambda t, a: t.get_observability(
                 float(a["dec_deg"]),
                 float(a["ra_deg"]) if a.get("ra_deg") not in (None, "") else None),
             {"dec_deg": "declination in degrees",
              "ra_deg": "right ascension in degrees (optional)"}),
]

TOOL_SPECS_BY_NAME = {s.name: s for s in READONLY_TOOL_SPECS}


__all__ = [
    "Agent",
    "AgentResponse",
    "ToolCall",
    "ToolSpec",
    "READONLY_TOOL_SPECS",
    "TOOL_SPECS_BY_NAME",
]
