"""Observing preflight — one call that answers "can the telescope actually
start right now, and if not, exactly what is blocking it?".

This is the antidote to the silent-failure mode that makes an armed plan
"run" while nothing physically happens: every precondition the bring-up
sequencer depends on is checked up front and reported with a concrete fix.

It is exposed three ways:

* as the agent tool ``preflight`` (:mod:`dsa_operator.agent.control`), so the
  Claude brain can self-diagnose before arming or when asked "why is nothing
  happening?";
* as the per-turn situational snapshot injected into the control prompt;
* as a CLI — ``python -m dsa_operator.preflight`` — so an operator (or
  ``scripts/laptop.sh``) can sanity-check the config + live state at startup.

The checks, in plain terms:

* ``policy_mode_live``    — ``mode: live`` in ``config/policy.yaml``.
* ``promote:<action>``    — each bring-up action is in ``config/local.yaml``'s
  ``promote:`` list (otherwise it is a shadow no-op even in live mode).
* ``live_executor_wired`` — a real executor is attached (full console, not a
  shadow/test build).
* ``estop_clear``         — the e-stop is not engaged.
* ``agents_enabled``      — the dashboard has not locked agents out.
* ``hold_lease``          — THIS session holds the executor lease (only when a
  ``session_id`` is supplied).
* ``executor_not_pinned`` — the dashboard hasn't pinned control to someone else.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from dsa_operator.policy import Policy

#: The control actions the observing bring-up sequencer issues. Each must be
#: promoted for a live bring-up to actually move the array — keep this in sync
#: with :mod:`dsa_operator.observing.session` (``point -> fstable -> modes ->
#: start/restart -> warm -> arm``).
CRITICAL_BRINGUP: tuple[str, ...] = (
    "point_array",
    "build_fstable",
    "deploy_fstable",
    "start_fleet",
    "restart_all",
    "utc_start",
)


def _chk(name: str, ok: bool, detail: str, fix: str = "") -> dict[str, Any]:
    c: dict[str, Any] = {"name": name, "ok": bool(ok), "detail": detail}
    if fix and not ok:
        c["fix"] = fix
    return c


def policy_checks(
    policy: Policy, actions: Sequence[str] = CRITICAL_BRINGUP
) -> list[dict[str, Any]]:
    """Config-only checks (no etcd needed): global mode + per-action promotion.

    These are the checks an operator most often gets wrong (the empty/locked
    ``promote:`` list), and they can be validated from the working tree alone.
    """
    live = policy.mode == "live"
    checks = [
        _chk(
            "policy_mode_live",
            live,
            f"policy mode = {policy.mode}",
            "set `mode: live` in config/policy.yaml, then restart the console",
        )
    ]
    for a in actions:
        if a not in policy.actions:
            continue
        promoted = a in policy.promoted
        if live and promoted:
            detail = "will execute live"
        elif promoted:
            detail = "promoted (but mode is not live)"
        else:
            detail = "NOT promoted → shadow no-op even in live mode"
        checks.append(
            _chk(
                f"promote:{a}",
                promoted,
                detail,
                f"add `- {a}` under `promote:` in config/local.yaml",
            )
        )
    return checks


def observing_preflight(
    engine: Any,
    *,
    session_id: Optional[str] = None,
    plan_store: Any = None,
    actions: Sequence[str] = CRITICAL_BRINGUP,
) -> dict[str, Any]:
    """Full readiness report against a live :class:`ControlEngine`.

    When ``session_id`` is given, the lease check requires THIS session to hold
    it (the agent's case). Omit it for a non-session context (e.g. the CLI),
    where the lease is reported but not required.
    """
    pol: Policy = engine.policy
    checks = list(policy_checks(pol, actions))

    checks.append(
        _chk(
            "live_executor_wired",
            engine.has_live_executor,
            "live executor attached"
            if engine.has_live_executor
            else "no live executor in this build — everything is shadow",
            "run the full console (scripts/laptop.sh), not a shadow/test build",
        )
    )

    paused = engine.is_paused()
    checks.append(
        _chk(
            "estop_clear",
            not paused,
            "e-stop ENGAGED" if paused else "e-stop clear",
            "Resume in the Control tab to clear the e-stop",
        )
    )

    auth = engine.authority()
    checks.append(
        _chk(
            "agents_enabled",
            auth.agents_enabled,
            "agents LOCKED OUT from the dashboard"
            if not auth.agents_enabled
            else "agents enabled",
            "re-enable agent control on the dsa110-rt dashboard authority panel",
        )
    )

    holder = engine.lease.holder()
    holder_json = holder.to_json() if holder else None
    holder_actor = holder.actor if holder else None
    if session_id is not None:
        i_hold = bool(holder and holder.session_id == session_id)
        checks.append(
            _chk(
                "hold_lease",
                i_hold,
                "this session holds the lease"
                if i_hold
                else (
                    f"lease held by {holder_actor!r}"
                    if holder
                    else "lease is free (nobody holds it)"
                ),
                "Acquire the executor lease in the Control tab for this session",
            )
        )

    if auth.executor_email and holder and holder.actor != auth.executor_email:
        checks.append(
            _chk(
                "executor_not_pinned_away",
                False,
                f"dashboard pinned the executor to {auth.executor_email}",
                "only the pinned operator can execute, or clear the pin on the "
                "dashboard",
            )
        )

    plan_info: Optional[dict[str, Any]] = None
    if plan_store is not None:
        try:
            plan = plan_store.get()
        except Exception:  # noqa: BLE001
            plan = None
        if plan is None:
            plan_info = {"staged": False, "armed": False}
        else:
            plan_info = {
                "staged": True,
                "armed": bool(plan.armed),
                "n_segments": len(plan.segments),
            }

    not_promoted = [
        a for a in actions if a in pol.actions and a not in pol.promoted
    ]
    ready = all(c["ok"] for c in checks)
    blockers = [
        f"{c['name']}: {c.get('fix') or c['detail']}"
        for c in checks
        if not c["ok"]
    ]
    return {
        "ready_to_observe": ready,
        "mode": pol.mode,
        "promoted": sorted(pol.promoted),
        "bringup_actions_not_promoted": not_promoted,
        "lease_holder": holder_json,
        "plan": plan_info,
        "checks": checks,
        "blockers": blockers,
    }


# -- CLI --------------------------------------------------------------------

def _print_human(report: dict[str, Any]) -> None:
    ready = report.get("ready_to_observe")
    head = "READY to observe" if ready else "NOT ready to observe"
    print(f"dsa110-operator preflight: {head}")
    print(f"  mode: {report.get('mode')}", end="")
    if report.get("config_only"):
        print("   (config-only — live lease/e-stop/authority not checked)")
    else:
        h = report.get("lease_holder")
        print(f"   lease: {h['actor'] if h else 'free'}")
    print("  checks:")
    for c in report.get("checks", []):
        mark = "ok " if c["ok"] else "XX "
        line = f"    [{mark}] {c['name']}: {c['detail']}"
        print(line)
        if not c["ok"] and c.get("fix"):
            print(f"           fix: {c['fix']}")
    blockers = report.get("blockers") or []
    if blockers:
        print("  blockers:")
        for b in blockers:
            print(f"    - {b}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(
        prog="python -m dsa_operator.preflight",
        description="Can the telescope actually start right now? Validates "
        "config/policy.yaml + config/local.yaml and (optionally) the live "
        "lease / e-stop / dashboard authority.",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument(
        "--no-etcd",
        action="store_true",
        help="config-only: skip the live lease/e-stop/authority checks",
    )
    args = ap.parse_args(argv)

    from dsa_operator.policy import PolicyConfigError, load_policy

    try:
        policy = load_policy()
    except PolicyConfigError as exc:
        print(f"CONFIG ERROR\n{exc}", file=sys.stderr)
        return 2

    report: Optional[dict[str, Any]] = None
    if not args.no_etcd:
        try:
            from dsa_operator.web.app import (
                _default_audit,
                _default_control_engine,
            )

            engine = _default_control_engine(_default_audit())
            report = observing_preflight(engine)
        except Exception as exc:  # noqa: BLE001 - etcd/dashboard may be down
            print(
                f"(note: live etcd/dashboard checks skipped — {exc})\n"
                "      run via the SSH tunnel for lease/e-stop/authority "
                "checks, or pass --no-etcd to silence this.",
                file=sys.stderr,
            )

    if report is None:
        checks = policy_checks(policy)
        ready = all(c["ok"] for c in checks)
        report = {
            "ready_to_observe": ready,
            "mode": policy.mode,
            "promoted": sorted(policy.promoted),
            "config_only": True,
            "checks": checks,
            "blockers": [
                f"{c['name']}: {c.get('fix') or c['detail']}"
                for c in checks
                if not c["ok"]
            ],
        }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report)
    return 0 if report.get("ready_to_observe") else 1


__all__ = ["CRITICAL_BRINGUP", "policy_checks", "observing_preflight", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
