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

import json
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
    payload: dict[str, Any] = {"cmd": "move", "val": round(el, 4)}
    refants = params.get("refants")
    if isinstance(refants, (list, tuple)):
        payload["refants"] = [int(x) for x in refants]
    steps = [Step(
        kind="etcd_put",
        target="/cmd/ant/<all>",     # executor expands to /cnf/corr antenna_order
        payload=payload,
        note=f"put_dict /cmd/ant/<n> {{'cmd':'move','val':{el:.4f}}} per antenna; "
             f"then poll /mon/ant/<n>.drv_state==2 until settled",
    )]
    return Plan("point_array", steps,
                f"Slew array to dec={dec:.3f} deg (el={el:.3f} deg).")


def _plan_fire_injection(params: dict[str, Any], policy: Policy) -> Plan:
    # Delegated to the dashboard /control/inject route, which validates the
    # payload, computes apply_at_specnum, fans out to /cmd/dsart/corr/<cg>/
    # inject, and publishes the /cnf/inject/active registry.
    form: dict[str, Any] = {}
    for k in ("dm_pc_cm3", "l_rad", "m_rad", "width_samples", "fluence_jy_ms",
              "target_snr", "profile", "chgroups", "margin_blocks"):
        if k in params:
            form[k] = params[k]
    snr = form.get("target_snr")
    dm = form.get("dm_pc_cm3")
    return Plan("fire_injection", [Step(
        kind="dashboard_post", target="/control/inject", payload=form,
        note="synthetic FRB; dashboard computes apply_at_specnum + registry")],
        f"Fire injection (target_snr={snr or '?'}, dm={dm or '?'}).")


def _plan_set_dumps_enabled(params: dict[str, Any], policy: Policy) -> Plan:
    enabled = _req_bool(params, "enabled")
    return Plan("set_dumps_enabled", [Step(
        kind="dashboard_post", target="/control/dumps_enabled",
        payload={"enabled": "true" if enabled else "false",
                 "confirm": "enable" if enabled else "suppress",
                 "reason": str(params.get("reason", "operator"))},
        note="etcd /cmd/c2/dumps_enabled via dashboard (audited)")],
        f"Set C2 dumps enabled = {enabled}.")


def _plan_dump_now(params: dict[str, Any], policy: Policy) -> Plan:
    return Plan("dump_now", [Step(
        kind="dashboard_post", target="/control/dump_now",
        payload={"confirm": "dump_now"},
        note="UDP C2 trigger to the search halves")],
        "Trigger a manual dump_now.")


def _plan_inject_calibrate(params: dict[str, Any], policy: Policy) -> Plan:
    form = {k: params[k] for k in (
        "dm_pc_cm3", "l_rad", "m_rad", "width_samples", "fluence_jy_ms",
        "profile", "chgroups", "use_ladder", "fluence_ladder", "health_check",
    ) if k in params}
    return Plan("inject_calibrate", [Step(
        kind="dashboard_post", target="/control/inject_calibrate", payload=form,
        note="fire + poll match; store K at /cnf/inject/snr_calibration")],
        "Run a calibration injection.")


def _plan_utc_start(params: dict[str, Any], policy: Policy) -> Plan:
    margin = int(params.get("margin", 30000))
    return Plan("utc_start", [Step(
        kind="dashboard_post", target="/control/utc_start",
        payload={"margin": margin},
        note="dashboard computes ARM_SEQ from capture last_seq_no")],
        f"Arm recording (utc_start, margin={margin}).")


def _plan_utc_stop(params: dict[str, Any], policy: Policy) -> Plan:
    return Plan("utc_stop", [Step(
        kind="dashboard_post", target="/control/utc_stop", payload={})],
        "Disarm recording (utc_stop).")


def _plan_start_fleet(params: dict[str, Any], policy: Policy) -> Plan:
    form: dict[str, Any] = {}
    if "dec_deg" in params:
        form["obs_dec_deg"] = float(params["dec_deg"])
    return Plan("start_fleet", [Step(
        kind="dashboard_post", target="/control/start", payload=form,
        note="ssh cleanup + etcd start on /cmd/corr_rt/0 and search fanout")],
        "Start the pipeline fleet.")


def _plan_stop_fleet(params: dict[str, Any], policy: Policy) -> Plan:
    return Plan("stop_fleet", [Step(
        kind="dashboard_post", target="/control/stop",
        payload={"confirm": "stop"},
        note="etcd stop on /cmd/corr_rt/0 + fanout")],
        "Stop the pipeline fleet.")


def _plan_restart_all(params: dict[str, Any], policy: Policy) -> Plan:
    # The dashboard /control/restart_all kicks off a background fanout (etcd +
    # ssh + lxc + systemctl) and returns 202 + a job_id; the bring-up
    # sequencer then waits on system_state rather than polling the job.
    form: dict[str, Any] = {"confirm": "restart_all"}
    if "dec_deg" in params:
        form["obs_dec_deg"] = float(params["dec_deg"])
    return Plan("restart_all", [Step(
        kind="dashboard_post", target="/control/restart_all", payload=form,
        note="async cold fleet restart; poll /control/system_state for warm")],
        "Cold-restart the whole fleet.")


def _plan_bounce_search(params: dict[str, Any], policy: Policy) -> Plan:
    form: dict[str, Any] = {"confirm": "bounce_search"}
    if "cn_ids" in params:
        form["cn_ids"] = params["cn_ids"]
    return Plan("bounce_search", [Step(
        kind="dashboard_post", target="/control/bounce_search", payload=form,
        note="search fanout stop -> sleep -> start on /cmd/search_rt/<cn>")],
        "Bounce search node(s).")


def _plan_build_fstable(params: dict[str, Any], policy: Policy) -> Plan:
    dec = _req_float(params, "dec_deg")
    form: dict[str, Any] = {"dec_deg": dec}
    if params.get("force"):
        form["force"] = "true"
    return Plan("build_fstable", [Step(
        kind="dashboard_post", target="/control/fstables/build", payload=form,
        note="subprocess build_fstable_cache.py in casa38 on h23")],
        f"Build fstable for dec={dec:.4f}.")


def _plan_deploy_fstable(params: dict[str, Any], policy: Policy) -> Plan:
    fn = params.get("filename")
    if not fn or "/" in str(fn):
        raise VerbError("deploy_fstable requires a bare 'filename' (.npz basename)")
    return Plan("deploy_fstable", [Step(
        kind="dashboard_post", target="/control/fstables/deploy",
        payload={"filename": str(fn)},
        note="rsync the fstable to the corr nodes")],
        f"Deploy fstable {fn} to corr nodes.")


def _plan_set_spectral_line(params: dict[str, Any], policy: Policy) -> Plan:
    subbands = params.get("subbands")
    if subbands is None:
        raise VerbError("set_spectral_line requires 'subbands'")
    if not isinstance(subbands, str):
        subbands = json.dumps(subbands)
    return Plan("set_spectral_line", [Step(
        kind="dashboard_post", target="/control/spectral_line",
        payload={"subbands": subbands, "confirm": "spectral_line",
                 "reason": str(params.get("reason", "operator"))},
        note="etcd /cnf/spectral_line; takes effect at next fleet start")],
        "Set spectral-line mode (next fleet start).")


def _plan_delete_snr_cal(params: dict[str, Any], policy: Policy) -> Plan:
    return Plan("delete_snr_cal", [Step(
        kind="dashboard_post", target="/control/delete_snr_cal",
        payload={"confirm": "delete_snr_cal"},
        note="etcd delete-prefix /cnf/inject/snr_calibration/*")],
        "Delete the SNR calibration.")


def _plan_update_fleet_code(params: dict[str, Any], policy: Policy) -> Plan:
    form: dict[str, Any] = {"confirm": "update_dsart"}
    for k in ("branch", "hosts"):
        if k in params:
            form[k] = params[k]
    return Plan("update_fleet_code", [Step(
        kind="dashboard_post", target="/control/update_dsart", payload=form,
        note="ALWAYS human-approved; ssh git fanout across the fleet")],
        f"Update fleet code (branch={form.get('branch', 'default')}).")


def _plan_set_policy(params: dict[str, Any], policy: Policy) -> Plan:
    # Editing the operator's own capability policy is a LOCAL, two-person
    # action; the live executor deliberately cannot perform it (it has no
    # such step kind), so it must be done by hand even if promoted.
    return Plan("set_policy", [Step(
        kind="local_policy_edit", target="config/policy.yaml",
        payload=dict(params or {}),
        note="two-person; performed by hand — not by the live executor")],
        "Edit the operator capability policy (two-person, manual).")


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
    "inject_calibrate": _plan_inject_calibrate,
    "utc_start": _plan_utc_start,
    "utc_stop": _plan_utc_stop,
    "set_dumps_enabled": _plan_set_dumps_enabled,
    "dump_now": _plan_dump_now,
    "start_fleet": _plan_start_fleet,
    "stop_fleet": _plan_stop_fleet,
    "bounce_search": _plan_bounce_search,
    "restart_all": _plan_restart_all,
    "build_fstable": _plan_build_fstable,
    "deploy_fstable": _plan_deploy_fstable,
    "set_spectral_line": _plan_set_spectral_line,
    "delete_snr_cal": _plan_delete_snr_cal,
    "update_fleet_code": _plan_update_fleet_code,
    "set_policy": _plan_set_policy,
}

REGISTRY: dict[str, Verb] = {name: Verb(name, b) for name, b in _BUILDERS.items()}


def get_verb(action: str) -> Optional[Verb]:
    return REGISTRY.get(action)


__all__ = ["Verb", "Plan", "Step", "VerbError", "REGISTRY", "get_verb",
           "dec_to_el", "N_ANTS_HINT"]
