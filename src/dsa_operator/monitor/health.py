"""Standing health evaluation (Phase 5).

Deterministic, read-only health assessment built entirely on the agent's
:class:`~dsa_operator.tools.readonly.ReadOnlyTools` surface. It polls the
fleet, static-sky monitor, SEFD scanner, and the live observation-time
status, applies configured thresholds, and rolls everything up into a
:class:`HealthReport` of findings.

No LLM, no writes — this is the trustworthy substrate the autonomy
supervisor reasons over. Every probe is wrapped so one flaky endpoint
produces a ``tool_error`` finding rather than crashing the whole report.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Levels, ordered worst-last so max() picks the most severe.
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_ALERT = "alert"
_RANK = {LEVEL_OK: 0, LEVEL_WARN: 1, LEVEL_ALERT: 2}


@dataclass(frozen=True)
class HealthThresholds:
    fleet_min_corr: int = 16
    fleet_min_search: int = 16
    sky_frame_max_age_s: float = 300.0
    sefd_max_age_s: float = 3600.0

    @classmethod
    def from_policy_autonomy(cls, autonomy: dict[str, Any]) -> "HealthThresholds":
        t = dict((autonomy or {}).get("thresholds", {}) or {})
        return cls(
            fleet_min_corr=int(t.get("fleet_min_corr", 16)),
            fleet_min_search=int(t.get("fleet_min_search", 16)),
            sky_frame_max_age_s=float(t.get("sky_frame_max_age_s", 300.0)),
            sefd_max_age_s=float(t.get("sefd_max_age_s", 3600.0)),
        )


@dataclass
class HealthFinding:
    level: str
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code,
                "message": self.message, "details": self.details}


@dataclass
class HealthReport:
    ts: float
    findings: list[HealthFinding] = field(default_factory=list)

    @property
    def level(self) -> str:
        if not self.findings:
            return LEVEL_OK
        return max((f.level for f in self.findings), key=lambda lv: _RANK.get(lv, 0))

    def by_level(self, level: str) -> list[HealthFinding]:
        return [f for f in self.findings if f.level == level]

    @property
    def alerts(self) -> list[HealthFinding]:
        return self.by_level(LEVEL_ALERT)

    @property
    def codes(self) -> set[str]:
        return {f.code for f in self.findings if f.level != LEVEL_OK}

    def to_json(self) -> dict[str, Any]:
        return {"ts": self.ts, "level": self.level,
                "findings": [f.to_json() for f in self.findings]}


def _age(now: float, ts: Optional[float]) -> Optional[float]:
    if ts is None:
        return None
    try:
        return max(0.0, now - float(ts))
    except (TypeError, ValueError):
        return None


def _first_num(d: Any, *keys: str) -> Optional[float]:
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return None


def evaluate_health(
    tools: Any, *,
    thresholds: Optional[HealthThresholds] = None,
    now: Optional[float] = None,
    observation: Any = None,
) -> HealthReport:
    """Build a :class:`HealthReport` from the read-only tool surface.

    ``observation`` is an optional :class:`ObservationStatus` (from the
    control engine) so an over-the-cap recording surfaces as an alert.
    """
    th = thresholds or HealthThresholds()
    t = now if now is not None else time.time()
    report = HealthReport(ts=t)

    def add(level, code, message, **details):
        report.findings.append(HealthFinding(level, code, message, details))

    # -- fleet -------------------------------------------------------------
    try:
        fleet = tools.get_fleet_status()
        corr = (fleet or {}).get("corr", {})
        search = (fleet or {}).get("search", {})
        corr_rep = int(corr.get("n_reporting", 0))
        search_rep = int(search.get("n_reporting", 0))
        if corr_rep < th.fleet_min_corr:
            add(LEVEL_ALERT, "corr_nodes_down",
                f"only {corr_rep}/{corr.get('n_total', '?')} corr nodes reporting",
                down=corr.get("down", []), n_reporting=corr_rep)
        if search_rep < th.fleet_min_search:
            add(LEVEL_ALERT, "search_nodes_down",
                f"only {search_rep}/{search.get('n_total', '?')} search nodes reporting",
                down=search.get("down", []), n_reporting=search_rep)
        if corr_rep >= th.fleet_min_corr and search_rep >= th.fleet_min_search:
            add(LEVEL_OK, "fleet_ok", "all fleet nodes reporting",
                corr=corr_rep, search=search_rep)
    except Exception as exc:                                   # noqa: BLE001
        add(LEVEL_WARN, "tool_error", f"get_fleet_status failed: {exc}",
            tool="get_fleet_status")

    # -- static-sky monitor freshness -------------------------------------
    try:
        sky = tools.get_sky_status()
        ts = _first_num(sky, "latest_frame_unix", "latest_unix", "ts", "time")
        age = _age(t, ts)
        if age is None:
            add(LEVEL_WARN, "sky_no_data", "no static-sky frame timestamp available")
        elif age > th.sky_frame_max_age_s:
            add(LEVEL_WARN, "sky_stale",
                f"static-sky frame is {age:.0f}s old (>{th.sky_frame_max_age_s:.0f}s)",
                age_s=age)
        else:
            add(LEVEL_OK, "sky_ok", f"static-sky frame {age:.0f}s old", age_s=age)
    except Exception as exc:                                   # noqa: BLE001
        add(LEVEL_WARN, "tool_error", f"get_sky_status failed: {exc}",
            tool="get_sky_status")

    # -- SEFD scanner freshness -------------------------------------------
    try:
        sefd = tools.get_sefd()
        ts = _first_num(sefd, "last_update", "ts", "time", "unix_ts", "mjd")
        age = _age(t, ts)
        if age is not None and age > th.sefd_max_age_s:
            add(LEVEL_WARN, "sefd_stale",
                f"SEFD scan is {age:.0f}s old (>{th.sefd_max_age_s:.0f}s)", age_s=age)
    except Exception as exc:                                   # noqa: BLE001
        add(LEVEL_WARN, "tool_error", f"get_sefd failed: {exc}", tool="get_sefd")

    # -- observation-time cap (dashboard-asserted) ------------------------
    if observation is not None:
        try:
            if getattr(observation, "overrun", False):
                add(LEVEL_ALERT, "obs_overrun",
                    f"observation has run {getattr(observation, 'elapsed_s', '?')}s, "
                    f"over the {getattr(observation, 'max_obs_seconds', '?')}s cap",
                    elapsed_s=getattr(observation, "elapsed_s", None),
                    cap_s=getattr(observation, "max_obs_seconds", None))
        except Exception:                                      # noqa: BLE001
            pass

    return report


__all__ = [
    "LEVEL_OK", "LEVEL_WARN", "LEVEL_ALERT",
    "HealthThresholds", "HealthFinding", "HealthReport", "evaluate_health",
]
