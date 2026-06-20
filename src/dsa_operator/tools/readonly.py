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

    # -- deeper fleet / data-quality rollups ---------------------------------
    def get_capture_health(self) -> dict[str, Any]:
        """UDP capture health across corr nodes: writing?, kernel drops,
        degraded streams, data rate (gbps)."""
        self._guard("get_capture_health")
        rows = []
        for cn in CORR_CNS:
            for port in (4011, 4012):
                d = self._etcd.get_dict(f"/mon/corr_rt/{cn}/capture/{port}")
                if isinstance(d, dict) and d:
                    rows.append((cn, port, d))
        rates = [d["rate_gbps"] for _, _, d in rows
                 if isinstance(d.get("rate_gbps"), (int, float))]
        drops = [{"cn": cn, "port": port, "pps": d.get("rate_kernel_drop_pps")}
                 for cn, port, d in rows if d.get("rate_kernel_drop_pps")]
        degraded = [{"cn": cn, "port": port} for cn, port, d in rows
                    if d.get("degraded")]
        writing = sum(1 for _, _, d in rows if d.get("arm_state") == "WRITING")
        summary = {
            "n_streams": len(rows), "n_writing": writing,
            "n_kernel_dropping": len(drops), "kernel_drops": drops[:10],
            "n_degraded": len(degraded), "degraded": degraded[:10],
            "rate_gbps_min": round(min(rates), 2) if rates else None,
            "rate_gbps_max": round(max(rates), 2) if rates else None,
        }
        return self._ok("get_capture_health", {}, summary)

    def get_buffer_health(self) -> dict[str, Any]:
        """PSRDADA ring-buffer pressure across corr nodes (worst node per
        ring: dada/eada/fada/bada)."""
        self._guard("get_buffer_health")
        worst: dict[str, tuple] = {}
        for cn in CORR_CNS:
            d = self._etcd.get_dict(f"/mon/corr_rt/{cn}")
            bufs = d.get("buffers") if isinstance(d, dict) else None
            if not isinstance(bufs, dict):
                continue
            for name, b in bufs.items():
                m = b.get("metric") if isinstance(b, dict) else None
                if not isinstance(m, dict):
                    continue
                nb, nf = m.get("nbufs"), m.get("nfull")
                if isinstance(nb, (int, float)) and nb > 0 \
                        and isinstance(nf, (int, float)):
                    frac = nf / nb
                    if name not in worst or frac > worst[name][2]:
                        worst[name] = (cn, nf, frac, nb)
        rings = {name: {"node_cn": cn, "nfull": nf, "nbufs": nb,
                        "fill_frac": round(fr, 3)}
                 for name, (cn, nf, fr, nb) in worst.items()}
        return self._ok("get_buffer_health", {}, {"rings": rings})

    def get_warmup_status(self) -> dict[str, Any]:
        """Per corr node corr_fast warmup gate (ready == safe to arm)."""
        self._guard("get_warmup_status")
        ready, not_ready = [], []
        for cn in CORR_CNS:
            d = self._etcd.get_dict(f"/mon/corr_rt/{cn}/corr_fast_ready")
            if isinstance(d, dict) and d.get("ready"):
                ready.append(cn)
            elif isinstance(d, dict) and d:
                not_ready.append(cn)
        return self._ok("get_warmup_status", {}, {
            "n_ready": len(ready), "n_total_reporting": len(ready) + len(not_ready),
            "not_ready": not_ready})

    def get_rfi_detail(self) -> dict[str, Any]:
        """Per-node RFI flag fractions (deeper than the ring-health summary):
        fleet median/max total_flag_fraction + worst nodes."""
        self._guard("get_rfi_detail")
        per_node = []
        for cn in CORR_CNS:
            d = self._etcd.get_dict(f"/mon/corr_rt/{cn}/rfi")
            if not isinstance(d, dict) or not d:
                continue
            tf = d.get("total_flag_fraction")
            both = tf.get("both") if isinstance(tf, dict) else tf
            if isinstance(both, (int, float)):
                per_node.append({"cn": cn, "total_flag_fraction": round(both, 3),
                                 "age_s": d.get("age_s")})
        fracs = sorted(x["total_flag_fraction"] for x in per_node)
        median = fracs[len(fracs) // 2] if fracs else None
        worst = sorted(per_node, key=lambda x: -x["total_flag_fraction"])[:5]
        return self._ok("get_rfi_detail", {}, {
            "n_nodes": len(per_node), "flag_fraction_median": median,
            "flag_fraction_max": (fracs[-1] if fracs else None),
            "worst_nodes": worst})

    def get_search_health(self) -> dict[str, Any]:
        """Search compute/noise/dump health across search nodes: C1 metering
        drops, Layer-2 sigma-clamp deadlock, cube-dump drops / late triggers."""
        self._guard("get_search_health")
        metering, clamp, dump_drops, late = [], [], [], []
        for cn in SEARCH_CNS:
            for g in (0, 1):
                comp = self._etcd.get_dict(f"/mon/search_rt/{cn}/compute/{g}")
                if isinstance(comp, dict) and comp.get("c1_metering_active"):
                    metering.append({"cn": cn, "g": g,
                                     "frac": comp.get("c1_metering_frac")})
                noise = self._etcd.get_dict(f"/mon/search_rt/{cn}/noise/{g}")
                if isinstance(noise, dict):
                    cs = noise.get("clamp_streak_max")
                    if isinstance(cs, (int, float)) and cs > 0:
                        clamp.append({"cn": cn, "g": g, "clamp_streak_max": cs})
                dump = self._etcd.get_dict(f"/mon/search_rt/{cn}/dump/{g}")
                if isinstance(dump, dict):
                    if dump.get("cube_dump_n_dropped"):
                        dump_drops.append({"cn": cn, "g": g,
                                           "n": dump.get("cube_dump_n_dropped")})
                    if dump.get("c2_trigger_too_late"):
                        late.append({"cn": cn, "g": g,
                                     "n": dump.get("c2_trigger_too_late")})
        return self._ok("get_search_health", {}, {
            "c1_metering_active": metering, "sigma_clamp": clamp,
            "cube_dump_drops": dump_drops, "c2_trigger_too_late": late})

    def get_voltage_retention(self) -> dict[str, Any]:
        """Voltage-buffer retention window across corr nodes (for dump replay)."""
        self._guard("get_voltage_retention")
        rows = []
        for cn in CORR_CNS:
            d = self._etcd.get_dict(f"/mon/corr_rt/{cn}/voltage_retention")
            if isinstance(d, dict) and d:
                rows.append({"cn": cn, "retention_s": d.get("retention_s"),
                             "queue_depth": d.get("queue_depth")})
        rets = [r["retention_s"] for r in rows
                if isinstance(r["retention_s"], (int, float))]
        return self._ok("get_voltage_retention", {}, {
            "n_nodes": len(rows), "retention_s_min": min(rets) if rets else None,
            "nodes": rows})

    def get_c2_status(self) -> dict[str, Any]:
        """C2 coincidencer snapshot: trigger/dump counters, dumps_enabled,
        receiver health, last event, injection-match counters."""
        self._guard("get_c2_status")
        try:
            snap = self._dash.get("/control/c2_snapshot")
        except Exception as exc:                           # noqa: BLE001
            snap = {"error": str(exc)}
        return self._ok("get_c2_status", {}, snap)

    def get_services_status(self) -> dict[str, Any]:
        """Fleet systemd service table (active/inactive/failed per node)."""
        self._guard("get_services_status")
        try:
            st = self._dash.get("/control/services_status")
        except Exception as exc:                           # noqa: BLE001
            st = {"error": str(exc)}
        return self._ok("get_services_status", {}, st)

    def get_dumps_state(self) -> dict[str, Any]:
        """C2 voltage-dump kill-switch state (enabled? who/when/why)."""
        self._guard("get_dumps_state")
        try:
            st = self._dash.get("/control/dumps_enabled")
        except Exception as exc:                           # noqa: BLE001
            st = {"error": str(exc)}
        return self._ok("get_dumps_state", {}, st)

    def get_spectral_line_state(self) -> dict[str, Any]:
        """Per-chgroup spectral-line mode + integration settings."""
        self._guard("get_spectral_line_state")
        try:
            st = self._dash.get("/control/spectral_line")
        except Exception as exc:                           # noqa: BLE001
            st = {"error": str(exc)}
        return self._ok("get_spectral_line_state", {}, st)

    def get_inject_calibrations(self) -> dict[str, Any]:
        """SNR-calibration (K-factor) buckets per DM from injections."""
        self._guard("get_inject_calibrations")
        try:
            st = self._dash.get("/control/inject_calibrations")
        except Exception as exc:                           # noqa: BLE001
            st = {"error": str(exc)}
        return self._ok("get_inject_calibrations", {}, st)

    def transit_report(self, sources: list,
                       beam_fwhm_deg: float = 3.0) -> dict[str, Any]:
        """Predicted meridian transits for sources YOU supply (look up their
        RA/Dec/DM yourself — there is no catalog), cross-checked against the
        current pointing and recent detections.

        Each source: ``{label, ra_deg, dec_deg, dm_pc_cm3?, expected_snr?}``.
        Returns, per source: next/previous transit (UTC), transit elevation,
        whether it is in the beam at the current pointing dec, and any recent
        candidate that matches the last transit (by time, and DM if given)."""
        params = {"n_sources": len(sources) if isinstance(sources, list) else 0}
        self._guard("transit_report", params)
        if not isinstance(sources, list) or not sources:
            raise ToolError("sources must be a non-empty list")
        import datetime as _dt
        import time as _time

        from dsa_operator.observing import astro
        pt = self._policy.pointing
        lat = float(pt.get("lat_ovro_deg", astro.OVRO_LAT_DEG))
        el_min = float(pt.get("el_min_deg", 30.0))
        el_max = float(pt.get("el_max_deg", 125.0))
        half = float(beam_fwhm_deg) / 2.0
        sidereal_day = 86164.0905
        now = _time.time()
        # current pointing dec
        try:
            point_dec = (self.get_array_pointing() or {}).get("target_dec_deg")
            point_dec = float(point_dec) if point_dec is not None else None
        except Exception:                                  # noqa: BLE001
            point_dec = None
        # recent candidates for reconciliation
        try:
            ev = self._dash.get("/control/recent_events")
            cands = ev.get("events", ev) if isinstance(ev, dict) else ev
            cands = cands if isinstance(cands, list) else []
        except Exception:                                  # noqa: BLE001
            cands = []

        def _match(prev_unix: float, dm: Optional[float]) -> Optional[dict]:
            best = None
            for c in cands:
                if not isinstance(c, dict):
                    continue
                mjd = c.get("mjd_peak") or c.get("mjd")
                if mjd is None:
                    continue
                try:
                    cu = astro.mjd_to_unix(float(mjd))
                except (TypeError, ValueError):
                    continue
                if abs(cu - prev_unix) > 300.0:       # within 5 min of transit
                    continue
                if dm is not None:
                    cdm = c.get("dm_median") or c.get("dm")
                    if isinstance(cdm, (int, float)) and abs(cdm - dm) > max(30.0, 0.2 * dm):
                        continue
                cand = {"name": c.get("name"), "snr_max": c.get("snr_max"),
                        "dm_median": c.get("dm_median"),
                        "trigger_class": c.get("trigger_class"),
                        "dt_s": round(cu - prev_unix, 1)}
                if best is None or (cand.get("snr_max") or 0) > (best.get("snr_max") or 0):
                    best = cand
            return best

        out = []
        for s in sources:
            try:
                ra = float(s["ra_deg"]); dec = float(s["dec_deg"])
            except (KeyError, TypeError, ValueError):
                raise ToolError("each source needs numeric ra_deg and dec_deg")
            dm = s.get("dm_pc_cm3")
            dm = float(dm) if isinstance(dm, (int, float)) else None
            nt = astro.next_transit_unix(ra, now)
            pt_unix = nt - sidereal_day
            el = astro.dec_to_el(dec, lat)
            in_beam = (point_dec is not None and abs(dec - point_dec) <= half)
            rec = {
                "label": s.get("label", ""), "ra_deg": ra, "dec_deg": dec,
                "dm_pc_cm3": dm,
                "transit_el_deg": round(el, 3),
                "observable": astro.is_observable(dec, el_min=el_min,
                                                  el_max=el_max, lat_deg=lat),
                "in_beam_now": in_beam,
                "dec_offset_from_pointing_deg": (round(dec - point_dec, 3)
                                                 if point_dec is not None else None),
                "next_transit_utc": _dt.datetime.utcfromtimestamp(nt).isoformat() + "Z",
                "seconds_to_next_transit": round(nt - now, 1),
                "last_transit_utc": _dt.datetime.utcfromtimestamp(pt_unix).isoformat() + "Z",
                "detected_last_transit": _match(pt_unix, dm),
            }
            if s.get("expected_snr") is not None and rec["detected_last_transit"]:
                try:
                    obs = rec["detected_last_transit"].get("snr_max")
                    exp = float(s["expected_snr"])
                    if isinstance(obs, (int, float)) and exp > 0:
                        rec["snr_ratio_obs_over_expected"] = round(obs / exp, 2)
                except (TypeError, ValueError):
                    pass
            out.append(rec)
        return self._ok("transit_report", params, {
            "now_utc": _dt.datetime.utcfromtimestamp(now).isoformat() + "Z",
            "pointing_dec_deg": point_dec, "beam_fwhm_deg": float(beam_fwhm_deg),
            "sources": out})

    def health_report(self) -> dict[str, Any]:
        """One comprehensive 'is the telescope working, and how well' rollup:
        fleet, pointing, capture/drops, buffers, RFI, search, SEFD, injections,
        candidates, sky, dumps — each with an ok/warn/alert level and a short
        line, plus an overall level."""
        self._guard("health_report")
        sections: dict[str, dict[str, Any]] = {}
        levels = {"ok": 0, "warn": 1, "alert": 2}

        def add(name: str, level: str, line: str, **extra) -> None:
            sections[name] = {"level": level, "line": line, **extra}

        def safe(fn):
            try:
                r = fn()
                return r if isinstance(r, dict) else {"value": r}
            except Exception as exc:                       # noqa: BLE001
                return {"error": str(exc)}

        fleet = safe(self.get_fleet_status)
        corr = fleet.get("corr", {}); search = fleet.get("search", {})
        ss = fleet.get("system_state", {})
        n_corr = corr.get("n_reporting", 0); n_search = search.get("n_reporting", 0)
        lvl = "ok" if (n_corr >= len(CORR_CNS) and n_search >= len(SEARCH_CNS)) else "alert"
        add("fleet", lvl,
            f"{n_corr}/{len(CORR_CNS)} corr, {n_search}/{len(SEARCH_CNS)} search up; "
            f"state={ss.get('state')}, safe_to_arm={ss.get('safe_to_arm')}",
            down_corr=corr.get("down", []), down_search=search.get("down", []))

        pt = safe(self.get_array_pointing)
        nns = pt.get("n_not_settled")
        add("pointing", "warn" if nns else "ok",
            f"dec={pt.get('target_dec_deg')}, mean_el={pt.get('mean_commanded_el_deg')}, "
            f"{nns} not settled")

        cap = safe(self.get_capture_health)
        cap_lvl = "alert" if cap.get("n_kernel_dropping") else (
            "warn" if cap.get("n_degraded") else "ok")
        add("capture", cap_lvl,
            f"{cap.get('n_writing')}/{cap.get('n_streams')} writing, "
            f"{cap.get('n_kernel_dropping')} kernel-dropping, "
            f"{cap.get('n_degraded')} degraded; rate "
            f"{cap.get('rate_gbps_min')}–{cap.get('rate_gbps_max')} Gbps")

        rfi = safe(self.get_rfi_detail)
        rmax = rfi.get("flag_fraction_max")
        rfi_lvl = "ok"
        if isinstance(rmax, (int, float)):
            rfi_lvl = "alert" if rmax > 0.7 else ("warn" if rmax > 0.4 else "ok")
        add("rfi", rfi_lvl,
            f"flag fraction median={rfi.get('flag_fraction_median')}, "
            f"max={rmax}")

        srch = safe(self.get_search_health)
        srch_lvl = "ok"
        if srch.get("sigma_clamp") or srch.get("cube_dump_drops") or srch.get("c2_trigger_too_late"):
            srch_lvl = "warn"
        add("search", srch_lvl,
            f"{len(srch.get('sigma_clamp', []))} sigma-clamp, "
            f"{len(srch.get('cube_dump_drops', []))} dump-drop, "
            f"{len(srch.get('c2_trigger_too_late', []))} late-trigger")

        sefd = safe(self.get_sefd)
        sefd_age = self._freshness(sefd) if isinstance(sefd, dict) else None
        add("sefd", "ok" if sefd.get("scanner_alive") else "warn",
            f"scanner_alive={sefd.get('scanner_alive')}, age_s={sefd.get('scanner_age_s')}")

        inj = safe(self.query_injections)
        matches = inj.get("matches", {})
        add("injections", "ok",
            f"{len(matches) if isinstance(matches, dict) else 0} recent match keys; "
            f"{len(inj.get('active', {})) if isinstance(inj.get('active'), dict) else 0} active")

        try:
            cand = self.list_candidates()
            if isinstance(cand, dict):
                cand = cand.get("events", cand)
            n_cand = len(cand) if isinstance(cand, list) else 0
        except Exception:                                  # noqa: BLE001
            n_cand = 0
        add("candidates", "ok", f"{n_cand} recent candidate(s)")

        sky = safe(self.get_sky_status)
        add("sky", "ok" if not sky.get("error") else "warn",
            "static-sky monitor reachable" if not sky.get("error") else str(sky.get("error")))

        dumps = safe(self.get_dumps_state)
        add("dumps", "ok",
            f"voltage dumps enabled={dumps.get('enabled')}")

        overall = max((levels[s["level"]] for s in sections.values()), default=0)
        overall_name = [k for k, v in levels.items() if v == overall][0]
        return self._ok("health_report", {}, {
            "overall": overall_name, "sections": sections})

    def describe_monitoring(self) -> dict[str, Any]:
        """Discovery: the full set of things you can ask about, the tool that
        answers each, and the underlying signal. Use this to answer 'what can
        you monitor?' and to pick the right tool."""
        self._guard("describe_monitoring")
        catalog = {
            "alive_and_running": {
                "tools": ["get_fleet_status", "get_services_status",
                          "get_warmup_status"],
                "signals": "corr(16)+search(4) heartbeats, systemd table, "
                           "system_state (offline/preparing/prepared/observing, "
                           "safe_to_arm), corr_fast warmup gate"},
            "pointing": {
                "tools": ["get_array_pointing", "get_observability",
                          "get_observing_plan", "get_fstable_status"],
                "signals": "target dec, mean elevation, antennas not settled, "
                           "transit/observability, active plan, fstable readiness"},
            "data_quality": {
                "tools": ["get_capture_health", "get_buffer_health",
                          "get_rfi_summary", "get_rfi_detail", "get_search_health"],
                "signals": "UDP rate/kernel-drops/degraded, PSRDADA ring fill, "
                           "RFI flag fractions, C1 metering, sigma-clamp, cube drops"},
            "sensitivity": {
                "tools": ["get_sefd", "get_inject_calibrations"],
                "signals": "SEFD per baseline band + coherence, K-factor buckets"},
            "detection_chain": {
                "tools": ["query_injections", "transit_report",
                          "list_candidates", "get_candidate", "get_c2_status",
                          "get_sky_status"],
                "signals": "injection fire/match/K, predicted source transits vs "
                           "detections, candidate stream, C2 triggers, static sky"},
            "configuration_and_audit": {
                "tools": ["get_dumps_state", "get_spectral_line_state",
                          "get_voltage_retention", "get_audit_log", "get_mon"],
                "signals": "dump kill-switch, spectral-line mode, voltage "
                           "retention, control audit, any /mon/ key"},
            "rollup": {
                "tools": ["health_report"],
                "signals": "one ok/warn/alert report card across all of the above"},
        }
        return self._ok("describe_monitoring", {}, catalog)

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
