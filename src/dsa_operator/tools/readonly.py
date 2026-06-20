"""Read-only tools — the agent's monitoring surface (Phase 0).

Each public method:
  * is named in ``policy.read_only`` (refused otherwise — defence in depth
    so even a buggy caller can't reach a non-allow-listed action),
  * takes validated, typed inputs (no free-form keys/paths/hosts),
  * composes etcd reads and dashboard GETs into a compact, JSON-able
    summary suitable for the model context (no raw telemetry dumps),
  * writes an audit record.

Nothing here can mutate observatory state: it holds a
:class:`~dsa_operator.etcd.read.ReadOnlyEtcd` (no put/lease/watch) and a
GET-only :class:`~dsa_operator.dashboard.DashboardClient`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from dsa_operator.audit.log import AuditLog
from dsa_operator.dashboard import DashboardClient
from dsa_operator.etcd.read import ReadOnlyEtcd
from dsa_operator.policy import Policy, load_policy

LOG = logging.getLogger("dsa_operator.tools.readonly")

# Production fleet topology (from dsa110-rt control_store / dsart_rt).
CORR_CNS = (3, 4, 5, 6, 7, 8, 10, 11, 12, 14, 15, 16, 18, 19, 21, 22)
SEARCH_CNS = (1, 2, 9, 13)


class ToolError(RuntimeError):
    """Raised for invalid tool inputs or disallowed actions."""


class ReadOnlyTools:
    def __init__(
        self,
        etcd: ReadOnlyEtcd,
        dashboard: DashboardClient,
        audit: AuditLog,
        *,
        policy: Optional[Policy] = None,
        actor: str = "system",
    ) -> None:
        self._etcd = etcd
        self._dash = dashboard
        self._audit = audit
        self._policy = policy or load_policy()
        self.actor = actor

    # -- internals ------------------------------------------------------------
    def _guard(self, action: str, params: Optional[dict[str, Any]] = None) -> None:
        if not self._policy.is_read_only_action(action):
            # Audit the refusal too — attempted out-of-policy calls matter.
            self._audit.read(action, actor=self.actor, ok=False,
                             params=params or {}, note="not in policy.read_only")
            raise ToolError(f"action {action!r} is not an allow-listed read")

    def _ok(self, action: str, params: dict[str, Any], result: Any) -> Any:
        self._audit.read(action, actor=self.actor, ok=True, params=params)
        return result

    @staticmethod
    def _freshness(d: Optional[dict[str, Any]]) -> Optional[float]:
        if not isinstance(d, dict):
            return None
        for k in ("time", "ts", "unix_ts", "last_update", "mjd"):
            if k in d:
                try:
                    return float(d[k])
                except (TypeError, ValueError):
                    return None
        return None

    # -- tools ----------------------------------------------------------------
    def get_fleet_status(self) -> dict[str, Any]:
        """Roll up per-node orchestrator heartbeats + the dashboard banner."""
        self._guard("get_fleet_status")
        corr = {cn: self._etcd.get_dict(f"/mon/service/corr_rt/{cn}") for cn in CORR_CNS}
        search = {cn: self._etcd.get_dict(f"/mon/service/search_rt/{cn}") for cn in SEARCH_CNS}

        def _alive(d: Any) -> bool:
            return isinstance(d, dict) and bool(d)

        summary = {
            "corr": {
                "n_reporting": sum(_alive(v) for v in corr.values()),
                "n_total": len(CORR_CNS),
                "down": [cn for cn, v in corr.items() if not _alive(v)],
            },
            "search": {
                "n_reporting": sum(_alive(v) for v in search.values()),
                "n_total": len(SEARCH_CNS),
                "down": [cn for cn, v in search.items() if not _alive(v)],
            },
        }
        try:
            summary["system_state"] = self._dash.get("/control/system_state")
        except Exception as exc:                           # noqa: BLE001
            summary["system_state"] = {"error": str(exc)}
        return self._ok("get_fleet_status", {}, summary)

    def get_array_pointing(self) -> dict[str, Any]:
        """Commanded array pointing: target dec + mean commanded elevation."""
        self._guard("get_array_pointing")
        dec = self._etcd.get_dict("/mon/array/dec")
        ant = self._etcd.get_prefix_dict("/mon/ant/")
        els: list[float] = []
        moving = 0
        for v in ant.values():
            if isinstance(v, dict):
                if "ant_cmd_el" in v:
                    try:
                        els.append(float(v["ant_cmd_el"]))
                    except (TypeError, ValueError):
                        pass
                if v.get("drv_state") not in (2, None):
                    moving += 1
        result = {
            "target_dec_deg": (dec or {}).get("dec_deg") if isinstance(dec, dict) else None,
            "n_antennas_reporting": sum(isinstance(v, dict) for v in ant.values()),
            "mean_commanded_el_deg": (sum(els) / len(els)) if els else None,
            "n_not_settled": moving,
        }
        return self._ok("get_array_pointing", {}, result)

    def get_mon(self, key: str) -> Any:
        """Read one ``/mon/...`` etcd key (scoped to /mon for read-only safety)."""
        params = {"key": key}
        self._guard("get_mon", params)
        if not isinstance(key, str) or not key.startswith("/mon/"):
            raise ToolError("get_mon only reads keys under '/mon/'")
        if ".." in key:
            raise ToolError("invalid key")
        return self._ok("get_mon", params, self._etcd.get_dict(key))

    def get_audit_log(self, n: int = 50) -> dict[str, Any]:
        """Recent control-audit rows (dashboard) + local operator audit tail."""
        self._guard("get_audit_log", {"n": n})
        n = max(1, min(int(n), 500))
        out: dict[str, Any] = {"local_tail": self._audit.tail(n)}
        try:
            out["dashboard_control_audit"] = self._dash.get("/control/recent_audit")
        except Exception as exc:                           # noqa: BLE001
            out["dashboard_control_audit"] = {"error": str(exc)}
        return self._ok("get_audit_log", {"n": n}, out)

    def list_candidates(self) -> dict[str, Any]:
        """Recent C2 candidate dirs (via dashboard recent-events JSON)."""
        self._guard("list_candidates")
        return self._ok("list_candidates", {}, self._dash.get("/control/recent_events"))

    def get_candidate(self, name: str) -> dict[str, Any]:
        """One candidate's summary, looked up in the recent-events list."""
        params = {"name": name}
        self._guard("get_candidate", params)
        if not isinstance(name, str) or "/" in name or ".." in name:
            raise ToolError("invalid candidate name")
        events = self._dash.get("/control/recent_events")
        items = events.get("events", events) if isinstance(events, dict) else events
        match = None
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("name") == name:
                    match = it
                    break
        return self._ok("get_candidate", params, {"name": name, "event": match})

    def get_sefd(self) -> dict[str, Any]:
        """SEFD scanner freshness + status."""
        self._guard("get_sefd")
        return self._ok("get_sefd", {}, self._dash.get("/api/sefd_status"))

    def get_rfi_summary(self) -> dict[str, Any]:
        """Per-chgroup RFI ring health from the dashboard."""
        self._guard("get_rfi_summary")
        return self._ok("get_rfi_summary", {}, self._dash.get("/api/status"))

    def get_sky_status(self) -> dict[str, Any]:
        """Static-sky monitor frame + per-chgroup snapshot freshness."""
        self._guard("get_sky_status")
        return self._ok("get_sky_status", {}, self._dash.get("/sky/status"))

    def query_injections(self) -> dict[str, Any]:
        """Active injections (etcd) + recent inject matches + C2 snapshot."""
        self._guard("query_injections")
        active = self._etcd.get_prefix_dict("/cnf/inject/active/")
        matches = self._etcd.get_prefix_dict("/mon/dsart/inject/matches/")
        result: dict[str, Any] = {
            "active": active,
            "matches": matches,
        }
        try:
            result["c2_snapshot"] = self._dash.get("/control/c2_snapshot")
        except Exception as exc:                           # noqa: BLE001
            result["c2_snapshot"] = {"error": str(exc)}
        return self._ok("query_injections", {}, result)

    def get_observing_plan(self) -> dict[str, Any]:
        """The active observing plan (operator namespace) + what's active now."""
        import time as _time

        from dsa_operator.observing.plan import PLAN_KEY, ObservingPlan

        self._guard("get_observing_plan")
        raw = self._etcd.get_dict(PLAN_KEY)
        if not isinstance(raw, dict) or not raw.get("segments"):
            return self._ok("get_observing_plan", {}, {"plan": None})
        plan = ObservingPlan.from_json(raw)
        now = _time.time()
        active = plan.active_at(now)
        nxt = plan.next_segment(now)
        result = {
            "plan": plan.to_json(),
            "n_segments": len(plan.segments),
            "active_now": active.to_json() if active else None,
            "dec_now": plan.dec_at(now),
            "next_segment": nxt.to_json() if nxt else None,
        }
        return self._ok("get_observing_plan", {}, result)

    def get_fstable_status(self, dec_deg: float) -> dict[str, Any]:
        """Fringe-stop-table traffic light for a declination (per corr node)."""
        params = {"dec_deg": dec_deg}
        self._guard("get_fstable_status", params)
        try:
            dec = float(dec_deg)
        except (TypeError, ValueError):
            raise ToolError("dec_deg must be a number")
        try:
            status = self._dash.get(
                f"/control/fstables/current_status?dec_deg={dec:.4f}")
        except Exception as exc:                           # noqa: BLE001
            status = {"error": str(exc)}
        return self._ok("get_fstable_status", params, status)

    def get_observability(self, dec_deg: float,
                          ra_deg: Optional[float] = None) -> dict[str, Any]:
        """Transit elevation / next-transit / observability for a dec (+optional RA)."""
        from dsa_operator.observing import astro

        params = {"dec_deg": dec_deg, "ra_deg": ra_deg}
        self._guard("get_observability", params)
        try:
            dec = float(dec_deg)
            ra = float(ra_deg) if ra_deg is not None else None
        except (TypeError, ValueError):
            raise ToolError("dec_deg (and ra_deg if given) must be numbers")
        pt = self._policy.pointing
        import time as _time

        obs = astro.observability(
            dec, ra_deg=ra, now_unix=_time.time(),
            el_min=float(pt.get("el_min_deg", 30.0)),
            el_max=float(pt.get("el_max_deg", 125.0)),
            lat_deg=float(pt.get("lat_ovro_deg", astro.OVRO_LAT_DEG)),
        )
        return self._ok("get_observability", params, obs.to_json())


def _demo(argv: Optional[list[str]] = None) -> int:
    """Live read-only smoke against the tunnel-forwarded services."""
    import argparse
    import json

    from dsa_operator.etcd.read import connect_readonly

    p = argparse.ArgumentParser(description="Phase-0 read-only smoke test.")
    p.add_argument("--etcd-port", type=int, default=None)
    p.add_argument("--dashboard-port", type=int, default=None)
    p.add_argument("--audit-root", default="audit_log")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dsa_operator import DEFAULT_LOCAL_DASHBOARD_PORT, DEFAULT_LOCAL_ETCD_PORT

    etcd = connect_readonly(port=args.etcd_port or DEFAULT_LOCAL_ETCD_PORT)
    dash = DashboardClient(port=args.dashboard_port or DEFAULT_LOCAL_DASHBOARD_PORT)
    audit = AuditLog(args.audit_root)
    tools = ReadOnlyTools(etcd, dash, audit, actor="demo")

    for name in ("get_fleet_status", "get_array_pointing", "get_sky_status"):
        try:
            print(f"== {name} ==")
            print(json.dumps(getattr(tools, name)(), indent=2, default=str)[:2000])
        except Exception as exc:                           # noqa: BLE001
            print(f"  {name} failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
