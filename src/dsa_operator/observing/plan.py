"""Observing plan: a timed schedule of declinations + persistence.

A plan is an ordered list of non-overlapping :class:`Segment`s, each
holding the array at one declination for a time window. Sources are
expressed by centring a window on their transit (when LST == RA). The plan
lives in etcd under the operator namespace (``/operator/plan/active``), so
it persists across operator restarts and is visible to anyone watching.

The plan is *intent*. Turning it into actual pointing is the
:class:`~dsa_operator.observing.runner.PlanRunner`'s job, and that always
goes through the gate engine — so a plan can never move the array by
itself.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from dsa_operator.observing import astro

PLAN_KEY = "/operator/plan/active"


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Segment:
    t_start: float
    t_end: float
    dec_deg: float
    label: str = ""
    ra_deg: Optional[float] = None     # set when the segment tracks a source

    def contains(self, t: float) -> bool:
        return self.t_start <= t < self.t_end

    def to_json(self) -> dict[str, Any]:
        d = {"t_start": self.t_start, "t_end": self.t_end,
             "dec_deg": self.dec_deg, "label": self.label}
        if self.ra_deg is not None:
            d["ra_deg"] = self.ra_deg
        return d

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Segment":
        return Segment(
            t_start=float(d["t_start"]), t_end=float(d["t_end"]),
            dec_deg=float(d["dec_deg"]), label=str(d.get("label", "")),
            ra_deg=(float(d["ra_deg"]) if d.get("ra_deg") is not None else None),
        )


@dataclass
class ObservingPlan:
    segments: list[Segment]
    created_by: str = "operator"
    created_ts: float = field(default_factory=time.time)
    note: str = ""

    # -- validation -----------------------------------------------------------
    def validate(self, *, el_min: float = 30.0, el_max: float = 125.0,
                 lat_deg: float = astro.OVRO_LAT_DEG) -> "ObservingPlan":
        segs = sorted(self.segments, key=lambda s: s.t_start)
        prev_end = None
        for s in segs:
            if s.t_end <= s.t_start:
                raise PlanError(f"segment {s.label!r} has t_end <= t_start")
            if not astro.is_observable(s.dec_deg, el_min=el_min, el_max=el_max,
                                       lat_deg=lat_deg):
                el = astro.dec_to_el(s.dec_deg, lat_deg)
                raise PlanError(
                    f"segment {s.label!r}: dec={s.dec_deg:.3f} -> el={el:.3f} "
                    f"outside [{el_min}, {el_max}]")
            if prev_end is not None and s.t_start < prev_end:
                raise PlanError(f"segment {s.label!r} overlaps the previous one")
            prev_end = s.t_end
        self.segments = segs
        return self

    # -- queries --------------------------------------------------------------
    def active_at(self, t: float) -> Optional[Segment]:
        for s in self.segments:
            if s.contains(t):
                return s
        return None

    def dec_at(self, t: float) -> Optional[float]:
        s = self.active_at(t)
        return s.dec_deg if s else None

    def next_segment(self, t: float) -> Optional[Segment]:
        upcoming = [s for s in self.segments if s.t_start > t]
        return min(upcoming, key=lambda s: s.t_start) if upcoming else None

    def span(self) -> Optional[tuple[float, float]]:
        if not self.segments:
            return None
        return self.segments[0].t_start, self.segments[-1].t_end

    # -- (de)serialise --------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "segments": [s.to_json() for s in self.segments],
            "created_by": self.created_by,
            "created_ts": self.created_ts,
            "note": self.note,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> "ObservingPlan":
        return ObservingPlan(
            segments=[Segment.from_json(s) for s in d.get("segments", [])],
            created_by=str(d.get("created_by", "operator")),
            created_ts=float(d.get("created_ts", time.time())),
            note=str(d.get("note", "")),
        )

    # -- builders -------------------------------------------------------------
    @staticmethod
    def from_segments(rows: Sequence[dict[str, Any]], *, created_by: str,
                      note: str = "") -> "ObservingPlan":
        return ObservingPlan([Segment.from_json(r) for r in rows],
                             created_by=created_by, note=note)

    @staticmethod
    def from_sources(rows: Sequence[dict[str, Any]], *, after_unix: float,
                     created_by: str, default_window_min: float = 30.0,
                     note: str = "") -> "ObservingPlan":
        """Build transit-centred segments from sources.

        Each row: ``{"label", "ra_deg", "dec_deg", "window_min"?}``. The
        segment is centred on the source's next transit after ``after_unix``,
        spanning ``window_min`` (default 30) minutes.
        """
        segs: list[Segment] = []
        for r in rows:
            ra = float(r["ra_deg"])
            dec = float(r["dec_deg"])
            win = float(r.get("window_min", default_window_min)) * 60.0
            tt = astro.next_transit_unix(ra, after_unix)
            segs.append(Segment(t_start=tt - win / 2.0, t_end=tt + win / 2.0,
                                dec_deg=dec, label=str(r.get("label", "")),
                                ra_deg=ra))
        return ObservingPlan(segs, created_by=created_by, note=note)


class PlanStore:
    """Persist the active plan in etcd (operator namespace).

    Writes via the prefix-guarded :class:`OperatorEtcdWriter` (so it lives
    under ``/operator/``) and reads via the read-only facade.
    """

    def __init__(self, writer: Any, reader: Any) -> None:
        self._w = writer
        self._r = reader

    def get(self) -> Optional[ObservingPlan]:
        raw = self._r.get_dict(PLAN_KEY)
        if not isinstance(raw, dict) or not raw.get("segments"):
            return None
        return ObservingPlan.from_json(raw)

    def set(self, plan: ObservingPlan) -> None:
        self._w.put(PLAN_KEY, plan.to_json())

    def clear(self) -> None:
        try:
            self._w.delete(PLAN_KEY)
        except Exception:                                  # noqa: BLE001
            self._w.put(PLAN_KEY, {"segments": []})


__all__ = ["Segment", "ObservingPlan", "PlanStore", "PlanError", "PLAN_KEY"]
