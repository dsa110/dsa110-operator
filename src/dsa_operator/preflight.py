"""``python -m dsa_operator.preflight`` entrypoint.

Thin shim over :mod:`dsa_operator.control.preflight` so the operator can run
the readiness check with the short, memorable module path.
"""
from __future__ import annotations

from dsa_operator.control.preflight import (
    CRITICAL_BRINGUP,
    main,
    observing_preflight,
    policy_checks,
)

__all__ = ["CRITICAL_BRINGUP", "policy_checks", "observing_preflight", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
