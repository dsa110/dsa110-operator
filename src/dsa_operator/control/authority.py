"""Human authority over the operator, asserted from the dsa110-rt dashboard.

A single etcd key, ``/cmd/operator/control``, lets humans on the dsa110-rt
dashboard hold three powers over the agent:

* ``agents_enabled`` — the master lockout. When ``false``, EVERY agent
  control attempt fails closed (reads/Q&A still work). This is a one-way
  human override.
* ``executor_email`` — optionally pin the single-executor right to one
  named Google identity. When set, only that user may hold the lease and
  act; when unset, the operator self-arbitrates via the lease.
* ``max_obs_seconds`` — the hard cap on how long one observation may run
  before the watchdog stops it.

Crucially this key lives **outside** every prefix the operator can write
(``OperatorEtcdWriter`` → ``/operator/``; ``ControlEtcdWriter`` →
``/cmd/ant/``). The agent can read it but can never enable itself,
re-point the executor, or lengthen its own time limit. Only a human (via
the dashboard, which writes this key) can.

Default when the key is absent/unreadable: enabled, unpinned, no cap — so
the operator works before the dashboard panel exists, while still being
fully lockable the moment a human sets the key.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

LOG = logging.getLogger("dsa_operator.control.authority")

AUTHORITY_KEY = "/cmd/operator/control"
ARMED_KEY = "/mon/snap/1/armed_mjd"

# MJD of the unix epoch (1970-01-01). Inlined to avoid importing the
# observing package here (it would create an import cycle through the
# runner -> engine -> authority).
_MJD_UNIX_EPOCH = 40587.0


def _mjd_to_unix(mjd: float) -> float:
    return (float(mjd) - _MJD_UNIX_EPOCH) * 86400.0


@dataclass(frozen=True)
class Authority:
    agents_enabled: bool = True
    executor_email: Optional[str] = None
    max_obs_seconds: Optional[float] = None
    by: str = ""
    ts: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "agents_enabled": self.agents_enabled,
            "executor_email": self.executor_email,
            "max_obs_seconds": self.max_obs_seconds,
            "by": self.by,
            "ts": self.ts,
        }


def read_authority(read_etcd: Any) -> Authority:
    """Read the dashboard authority key (fail-open to enabled/unpinned)."""
    if read_etcd is None:
        return Authority()
    try:
        d = read_etcd.get_dict(AUTHORITY_KEY)
    except Exception:                                      # noqa: BLE001
        LOG.warning("could not read %s; assuming enabled", AUTHORITY_KEY)
        return Authority()
    if not isinstance(d, dict):
        return Authority()
    email = d.get("executor_email") or None
    mx = d.get("max_obs_seconds")
    try:
        mx = float(mx) if mx not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        mx = None
    return Authority(
        agents_enabled=bool(d.get("agents_enabled", True)),
        executor_email=str(email) if email else None,
        max_obs_seconds=mx,
        by=str(d.get("by", "")),
        ts=float(d.get("ts", 0.0) or 0.0),
    )


@dataclass(frozen=True)
class ObservationStatus:
    armed: bool
    armed_unix: Optional[float] = None
    elapsed_s: Optional[float] = None
    max_obs_seconds: Optional[float] = None
    overrun: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "armed": self.armed,
            "armed_unix": self.armed_unix,
            "elapsed_s": (round(self.elapsed_s, 1)
                          if self.elapsed_s is not None else None),
            "max_obs_seconds": self.max_obs_seconds,
            "overrun": self.overrun,
            "remaining_s": (None if (self.max_obs_seconds is None
                                     or self.elapsed_s is None)
                            else round(self.max_obs_seconds - self.elapsed_s, 1)),
        }


def observation_status(read_etcd: Any, max_obs_seconds: Optional[float],
                       now_unix: float) -> ObservationStatus:
    """Whether recording is armed and, if so, for how long vs the cap.

    Reads the real armed epoch (``/mon/snap/1/armed_mjd``) that ``dsart_rt``
    publishes on ``utc_start`` — so the watchdog tracks the *observatory*,
    not the agent's belief about it.
    """
    armed_mjd = None
    if read_etcd is not None:
        try:
            d = read_etcd.get_dict(ARMED_KEY)
            if isinstance(d, dict):
                armed_mjd = d.get("armed_mjd")
        except Exception:                                  # noqa: BLE001
            armed_mjd = None
    if not armed_mjd:
        return ObservationStatus(armed=False, max_obs_seconds=max_obs_seconds)
    armed_unix = _mjd_to_unix(float(armed_mjd))
    elapsed = now_unix - armed_unix
    overrun = max_obs_seconds is not None and elapsed > max_obs_seconds
    return ObservationStatus(
        armed=True, armed_unix=armed_unix, elapsed_s=elapsed,
        max_obs_seconds=max_obs_seconds, overrun=overrun,
    )


__all__ = [
    "AUTHORITY_KEY", "ARMED_KEY",
    "Authority", "read_authority",
    "ObservationStatus", "observation_status",
]
