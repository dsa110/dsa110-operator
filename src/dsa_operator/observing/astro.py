"""Sidereal-time and transit math for DSA-110 (dependency-light).

No astropy: a meridian array pointed in declination doesn't need
arc-second astrometry to decide *when* a source transits or *what
elevation* a declination implies. We use the standard IAU 1982 GMST
polynomial (good to well under a second over the relevant epoch), which is
far more than enough.

Conventions:
* East longitude positive. OVRO is at roughly -118.283 deg.
* ``el = 90 - (lat - dec)`` is the commanded transit elevation, matching
  ``control_pointing.py`` (it can exceed 90 deg, pointing north past
  zenith — hence the >90 deg envelope ceiling).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# Site (OVRO / DSA-110). Latitude matches config/policy.yaml's pointing block.
OVRO_LON_DEG = -118.283400
OVRO_LAT_DEG = 37.23

# Mean sidereal rate: sidereal degrees per solar day.
_SIDEREAL_DEG_PER_DAY = 360.98564736629
_UNIX_EPOCH_JD = 2440587.5
_J2000_JD = 2451545.0


def jd_from_unix(unix_ts: float) -> float:
    return unix_ts / 86400.0 + _UNIX_EPOCH_JD


def gmst_deg(unix_ts: float) -> float:
    """Greenwich Mean Sidereal Time in degrees (IAU 1982)."""
    jd = jd_from_unix(unix_ts)
    d = jd - _J2000_JD
    t = d / 36525.0
    gmst = (280.46061837 + _SIDEREAL_DEG_PER_DAY * d
            + 0.000387933 * t * t - (t ** 3) / 38710000.0)
    return gmst % 360.0


def lst_deg(unix_ts: float, lon_deg: float = OVRO_LON_DEG) -> float:
    """Local (apparent ≈ mean) sidereal time in degrees at ``lon_deg``."""
    return (gmst_deg(unix_ts) + lon_deg) % 360.0


def next_transit_unix(ra_deg: float, after_unix: float,
                      lon_deg: float = OVRO_LON_DEG) -> float:
    """Unix time of the next meridian transit of ``ra_deg`` after ``after_unix``.

    A source transits when LST == RA. LST advances at the sidereal rate, so
    we step forward by the (wrapped) RA−LST gap converted to solar time.
    """
    lst = lst_deg(after_unix, lon_deg)
    delta_deg = (ra_deg - lst) % 360.0
    dt_days = delta_deg / _SIDEREAL_DEG_PER_DAY
    return after_unix + dt_days * 86400.0


def dec_to_el(dec_deg: float, lat_deg: float = OVRO_LAT_DEG) -> float:
    """Commanded transit elevation for a declination."""
    return 90.0 - (lat_deg - dec_deg)


def el_to_dec(el_deg: float, lat_deg: float = OVRO_LAT_DEG) -> float:
    return el_deg - 90.0 + lat_deg


def is_observable(dec_deg: float, *, el_min: float = 30.0,
                  el_max: float = 125.0, lat_deg: float = OVRO_LAT_DEG) -> bool:
    el = dec_to_el(dec_deg, lat_deg)
    return el_min <= el <= el_max


@dataclass(frozen=True)
class Observability:
    ra_deg: Optional[float]
    dec_deg: float
    lst_now_deg: float
    transit_el_deg: float
    observable: bool
    next_transit_unix: Optional[float]
    seconds_to_transit: Optional[float]

    def to_json(self) -> dict:
        return {
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "lst_now_deg": round(self.lst_now_deg, 4),
            "lst_now_hours": round(self.lst_now_deg / 15.0, 4),
            "transit_el_deg": round(self.transit_el_deg, 4),
            "observable": self.observable,
            "next_transit_unix": self.next_transit_unix,
            "seconds_to_transit": (round(self.seconds_to_transit, 1)
                                   if self.seconds_to_transit is not None else None),
        }


def observability(dec_deg: float, *, ra_deg: Optional[float] = None,
                  now_unix: float, el_min: float = 30.0, el_max: float = 125.0,
                  lat_deg: float = OVRO_LAT_DEG,
                  lon_deg: float = OVRO_LON_DEG) -> Observability:
    el = dec_to_el(dec_deg, lat_deg)
    obs = el_min <= el <= el_max
    nt = sec = None
    if ra_deg is not None:
        nt = next_transit_unix(ra_deg, now_unix, lon_deg)
        sec = nt - now_unix
    return Observability(
        ra_deg=ra_deg, dec_deg=dec_deg, lst_now_deg=lst_deg(now_unix, lon_deg),
        transit_el_deg=el, observable=obs, next_transit_unix=nt,
        seconds_to_transit=sec,
    )


__all__ = [
    "OVRO_LON_DEG", "OVRO_LAT_DEG",
    "jd_from_unix", "gmst_deg", "lst_deg", "next_transit_unix",
    "dec_to_el", "el_to_dec", "is_observable", "observability", "Observability",
]
