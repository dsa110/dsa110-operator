"""Typed control verbs: validation + a declarative *plan*.

A verb never executes anything itself. It validates its parameters and
returns a :class:`Plan` — an ordered list of :class:`Step` objects that
describe exactly the etcd puts / dashboard POSTs / SSH commands the action
*would* perform. In Phase 2 that plan is all that's produced (shadow mode);
Phase 3 hands the plan to a live executor behind the same gates.

Grounding: where the real wire format is already known from dsa110-rt it
is encoded here (pointing writes ``/cmd/ant/<ant>`` exactly as
``control_pointing.py`` does; injections write ``/cnf/inject/active/...``).
Verbs whose dsa110-rt endpoint still needs pinning carry a ``dashboard``
step with ``unverified=True`` so the gap is explicit and grep-able before
they are promoted to live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from dsa_operator.policy import Policy

# DSA-110 antenna ids that take elevation commands (from dsa110-controlscripts).
# Used only to describe the broadcast in a plan; not contacted in shadow.
N_ANTS_HINT = 117


class VerbError(ValueError):
    """Invalid parameters for a control verb."""


@dataclass(frozen=True)
class Step:
    kind: str                       # etcd_put | dashboard_post | ssh
    target: str                     # key / path / command
    payload: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    unverified: bool = False        # endpoint not yet pinned to dsa110-rt

    def to_json(self) -> dict[str, Any]:
        d = {"kind": self.kind, "target": self.target, "payload": self.payload}
        if self.note:
            d["note"] = self.note
        if self.unverified:
            d["unverified"] = True
        return d


@dataclass(frozen=True)
class Plan:
    action: str
    steps: list[Step]
    summary: str

    def to_json(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "summary": self.summary,
            "steps": [s.to_json() for s in self.steps],
            "n_steps": len(self.steps),
            "has_unverified": any(s.unverified for s in self.steps),
        }


# -- parameter helpers ------------------------------------------------------

def _req_float(params: dict[str, Any], key: str) -> float:
    if key not in params:
        raise VerbError(f"missing required parameter {key!r}")
    try:
        return float(params[key])
    except (TypeError, ValueError):
        raise VerbError(f"parameter {key!r} must be a number")


def _req_bool(params: dict[str, Any], key: str) -> bool:
    if key not in params:
        raise VerbError(f"missing required parameter {key!r}")
    v = params[key]
    if isinstance(v, bool):
        return v
    raise VerbError(f"parameter {key!r} must be a boolean")


def dec_to_el(dec_deg: float, lat_deg: float) -> float:
    """Meridian elevation for a target declination (transit pointing)."""
    return 90.0 - (lat_deg - dec_deg)


# -- plan builders ----------------------------------------------------------

def _plan_point_array(params: dict[str, Any], policy: Policy) -> Plan:
    dec = _req_float(params, "dec_deg")
    pt = policy.pointing
    lat = float(pt.get("lat_ovro_deg", 37.23))
    el_min = float(pt.get("el_min_deg", 30.0))
    el_max = float(pt.get("el_max_deg", 125.0))
    el = dec_to_el(dec, lat)
    if not (el_min <= el <= el_max):
        raise VerbError(
            f"dec={dec:.3f} deg -> el={el:.3f} deg is outside the allowed "
            f"[{el_min}, {el_max}] envelope")
    steps = [Step(
        kind="etcd_put",
        target="/cmd/ant/<all>",
        payload={"cmd": "move", "val": round(el, 4)},
        note=f"broadcast move to el={el:.3f} deg across ~{N_ANTS_HINT} antennas; "
             f"then poll /mon/ant/<n>.drv_state until settled",
    )]
    return Plan("point_array", steps,
                f"Slew array to dec={dec:.3f} deg (el={el:.3f} deg).")


def _plan_fire_injection(params: dict[str, Any], policy: Policy) -> Plan:
    snr = float(params.get("snr", 0) or 0)
    dm = float(params.get("dm", 0) or 0)
    payload = {k: params[k] for k in ("snr", "dm", "width_ms", "apply_at_specnum")
               if k in params}
    steps = [Step(
        kind="etcd_put",
        target="/cnf/inject/active/<id>",
        payload=payload or {"note": "default injection params"},
        note="register a synthetic FRB for corr_fast to inject; "
             "auto-expires after the apply window",
    )]
    return Plan("fire_injection", steps,
                f"Fire injection (snr={snr or '?'}, dm={dm or '?'}).")


def _plan_set_dumps_enabled(params: dict[str, Any], policy: Policy) -> Plan:
    enabled = _req_bool(params, "enabled")
    return Plan("set_dumps_enabled", [Step(
        kind="dashboard_post", target="/control/set_dumps_enabled",
        payload={"enabled": enabled}, unverified=True,
        note="toggle C2 cube dumping")],
        f"Set C2 dumps enabled = {enabled}.")


def _plan_dump_now(params: dict[str, Any], policy: Policy) -> Plan:
    return Plan("dump_now", [Step(
        kind="dashboard_post", target="/control/dump_now", payload={},
        unverified=True, note="force an immediate manual cube dump")],
        "Trigger a manual dump_now.")


def _simple_dashboard(action: str, path: str, summary: str) -> Callable:
    def build(params: dict[str, Any], policy: Policy) -> Plan:
        return Plan(action, [Step(
            kind="dashboard_post", target=path, payload=dict(params or {}),
            unverified=True)], summary)
    return build


def _plan_build_fstable(params: dict[str, Any], policy: Policy) -> Plan:
    dec = params.get("dec_deg")
    return Plan("build_fstable", [Step(
        kind="dashboard_post", target="/control/fstable/build",
        payload={"dec_deg": dec}, unverified=True,
        note="build the fringe-stopping table in casa38 on h23")],
        f"Build fstable for dec={dec}.")


def _plan_deploy_fstable(params: dict[str, Any], policy: Policy) -> Plan:
    dec = params.get("dec_deg")
    return Plan("deploy_fstable", [Step(
        kind="dashboard_post", target="/control/fstable/deploy",
        payload={"dec_deg": dec}, unverified=True,
        note="rsync the fstable to the corr nodes")],
        f"Deploy fstable for dec={dec} to corr nodes.")


def _plan_set_spectral_line(params: dict[str, Any], policy: Policy) -> Plan:
    return Plan("set_spectral_line", [Step(
        kind="dashboard_post", target="/control/set_spectral_line",
        payload=dict(params or {}), unverified=True,
        note="takes effect at the next fleet start")],
        "Set spectral-line mode (next fleet start).")


def _plan_update_fleet_code(params: dict[str, Any], policy: Policy) -> Plan:
    ref = params.get("ref", "origin/main")
    return Plan("update_fleet_code", [Step(
        kind="ssh", target="fleet: git fetch && git checkout",
        payload={"ref": ref}, unverified=True,
        note="ALWAYS human-approved; pull/checkout across the fleet")],
        f"Update fleet code to {ref}.")


def _plan_set_policy(params: dict[str, Any], policy: Policy) -> Plan:
    return Plan("set_policy", [Step(
        kind="ssh", target="edit config/policy.yaml",
        payload=dict(params or {}), unverified=True,
        note="two-person; edits the capability policy itself")],
        "Edit the operator capability policy (two-person).")


# -- registry ---------------------------------------------------------------

@dataclass(frozen=True)
class Verb:
    name: str
    build: Callable[[dict[str, Any], Policy], Plan]

    def plan(self, params: dict[str, Any], policy: Policy) -> Plan:
        return self.build(params or {}, policy)


_BUILDERS: dict[str, Callable[[dict[str, Any], Policy], Plan]] = {
    "point_array": _plan_point_array,
    "fire_injection": _plan_fire_injection,
    "inject_calibrate": _simple_dashboard(
        "inject_calibrate", "/control/inject_calibrate", "Run a calibration injection."),
    "utc_start": _simple_dashboard(
        "utc_start", "/control/utc_start", "Arm recording (ARM_SEQ)."),
    "utc_stop": _simple_dashboard(
        "utc_stop", "/control/utc_stop", "Disarm recording."),
    "set_dumps_enabled": _plan_set_dumps_enabled,
    "dump_now": _plan_dump_now,
    "start_fleet": _simple_dashboard(
        "start_fleet", "/control/start_fleet", "Start the pipeline fleet."),
    "stop_fleet": _simple_dashboard(
        "stop_fleet", "/control/stop_fleet", "Stop the pipeline fleet."),
    "bounce_search": _simple_dashboard(
        "bounce_search", "/control/bounce_search", "Bounce a search node."),
    "build_fstable": _plan_build_fstable,
    "deploy_fstable": _plan_deploy_fstable,
    "set_spectral_line": _plan_set_spectral_line,
    "delete_snr_cal": _simple_dashboard(
        "delete_snr_cal", "/control/delete_snr_cal", "Delete the SNR calibration."),
    "update_fleet_code": _plan_update_fleet_code,
    "set_policy": _plan_set_policy,
}

REGISTRY: dict[str, Verb] = {name: Verb(name, b) for name, b in _BUILDERS.items()}


def get_verb(action: str) -> Optional[Verb]:
    return REGISTRY.get(action)


__all__ = ["Verb", "Plan", "Step", "VerbError", "REGISTRY", "get_verb",
           "dec_to_el", "N_ANTS_HINT"]
