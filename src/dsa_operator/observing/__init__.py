"""Observing plans + pointing/astronomy helpers (Phase 4).

DSA-110 is a meridian transit instrument: it points only in elevation
(declination), and sources drift through the beam as the sky rotates. So
an "observing plan" is a timed schedule of declinations, and a source is
"observable" around its transit (when the local sidereal time equals its
right ascension).

* :mod:`dsa_operator.observing.astro` — dependency-light sidereal-time /
  transit math (no astropy needed; arc-second precision is irrelevant for
  a degree-scale beam pointed in dec).
* :mod:`dsa_operator.observing.plan` — the plan data model, validation,
  a transit-centred builder, and etcd persistence in the operator
  namespace.
* :mod:`dsa_operator.observing.runner` — turns the active plan into
  pointing actions, issued **through the ControlEngine** so they obey the
  same lease / e-stop / gate / approval / shadow rules as any other
  control. The runner never bypasses the gauntlet.
"""
from __future__ import annotations

from dsa_operator.observing.plan import ObservingPlan, PlanStore, Segment
from dsa_operator.observing.runner import PlanRunner

__all__ = ["ObservingPlan", "Segment", "PlanStore", "PlanRunner"]
