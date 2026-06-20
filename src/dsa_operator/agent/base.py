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
    # Optional explicit JSON-schema (for non-string args like lists/numbers);
    # when set it overrides the string-only schema derived from `params`.
    input_schema: dict[str, Any] = None  # type: ignore[assignment]


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
    # -- wider monitoring surface --------------------------------------------
    ToolSpec("describe_monitoring", "Discover everything you can monitor: the "
             "categories, the tool that answers each, and the underlying "
             "signal. Use first when asked 'what can you monitor?'.",
             lambda t, a: t.describe_monitoring()),
    ToolSpec("health_report", "One comprehensive ok/warn/alert report card "
             "across fleet, pointing, capture/drops, buffers, RFI, search, "
             "SEFD, injections, candidates, sky, dumps.",
             lambda t, a: t.health_report()),
    ToolSpec("get_capture_health", "UDP capture health across corr nodes: "
             "writing?, kernel drops, degraded streams, data rate.",
             lambda t, a: t.get_capture_health()),
    ToolSpec("get_buffer_health", "PSRDADA ring-buffer pressure across corr "
             "nodes (worst node per dada/eada/fada/bada ring).",
             lambda t, a: t.get_buffer_health()),
    ToolSpec("get_warmup_status", "Per corr node corr_fast warmup gate "
             "(ready == safe to arm).",
             lambda t, a: t.get_warmup_status()),
    ToolSpec("get_rfi_detail", "Per-node RFI flag fractions: fleet "
             "median/max + worst nodes (deeper than get_rfi_summary).",
             lambda t, a: t.get_rfi_detail()),
    ToolSpec("get_search_health", "Search compute/noise/dump health: C1 "
             "metering drops, Layer-2 sigma-clamp, cube-dump drops, late "
             "triggers.",
             lambda t, a: t.get_search_health()),
    ToolSpec("get_voltage_retention", "Voltage-buffer retention window across "
             "corr nodes (how far back a dump can reach).",
             lambda t, a: t.get_voltage_retention()),
    ToolSpec("get_c2_status", "C2 coincidencer snapshot: trigger/dump "
             "counters, dumps_enabled, receiver health, last event, "
             "injection-match counters.",
             lambda t, a: t.get_c2_status()),
    ToolSpec("get_services_status", "Fleet systemd service table "
             "(active/inactive/failed per node).",
             lambda t, a: t.get_services_status()),
    ToolSpec("get_dumps_state", "C2 voltage-dump kill-switch state "
             "(enabled? who/when/why).",
             lambda t, a: t.get_dumps_state()),
    ToolSpec("get_spectral_line_state", "Per-chgroup spectral-line mode and "
             "integration settings.",
             lambda t, a: t.get_spectral_line_state()),
    ToolSpec("get_inject_calibrations", "SNR-calibration (K-factor) buckets "
             "per DM from injections.",
             lambda t, a: t.get_inject_calibrations()),
    ToolSpec("get_fstable_status", "Fringe-stop-table traffic light for a "
             "declination (per corr node).",
             lambda t, a: t.get_fstable_status(float(a["dec_deg"])),
             {"dec_deg": "declination in degrees"}),
    ToolSpec("transit_report", "Predicted meridian transits for sources YOU "
             "supply (look up RA/Dec/DM yourself — no catalog), cross-checked "
             "against current pointing (in-beam?) and recent detections. Each "
             "source: {label, ra_deg, dec_deg, dm_pc_cm3?, expected_snr?}.",
             lambda t, a: t.transit_report(
                 a["sources"], beam_fwhm_deg=float(a.get("beam_fwhm_deg", 3.0))),
             input_schema={
                 "type": "object",
                 "properties": {
                     "sources": {"type": "array", "items": {
                         "type": "object",
                         "properties": {
                             "label": {"type": "string"},
                             "ra_deg": {"type": "number"},
                             "dec_deg": {"type": "number"},
                             "dm_pc_cm3": {"type": "number"},
                             "expected_snr": {"type": "number"}},
                         "required": ["ra_deg", "dec_deg"]}},
                     "beam_fwhm_deg": {"type": "number"}},
                 "required": ["sources"]}),
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
