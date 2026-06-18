"""Auto-recovery playbook (Phase 5).

Maps known-failure *signatures* (health-finding codes) to a proposed
recovery action. The playbook only *proposes* — it never executes. The
supervisor decides whether to submit a proposal, and even then it goes
through the full :class:`~dsa_operator.control.engine.ControlEngine`
gauntlet (lease, lockout, pause, gate, approval). So during commissioning
a proposed ``bounce_search`` surfaces as ``needs_approval``, not an
execution.

Each rule is deliberately conservative:

* Only *reversible* actions are ever marked ``auto`` (eligible for the
  supervisor to submit without a human prompting it).
* Failures with no safe automatic remedy (e.g. correlator nodes down,
  which usually means a host/hardware problem) propose **nothing** and are
  left as alerts for a human.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from dsa_operator.monitor.health import HealthReport


@dataclass
class RecoveryProposal:
    code: str                       # the health-finding code that triggered it
    action: str                     # control action to evaluate
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    auto: bool = False              # supervisor may submit without a human prompt

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "action": self.action, "params": self.params,
                "reason": self.reason, "auto": self.auto}


# A rule maps a finding code -> proposal factory (or None to skip).
_Rule = Callable[[dict[str, Any]], RecoveryProposal]


def _bounce_search(details: dict[str, Any]) -> RecoveryProposal:
    return RecoveryProposal(
        code="search_nodes_down", action="bounce_search", params={},
        reason=("search orchestrator(s) not reporting: "
                f"{details.get('down', [])} — restart the search half"),
        auto=True)


# Codes with NO safe automatic remedy: propose nothing, alert a human.
_NO_AUTO = {
    "corr_nodes_down",   # usually host/hardware; restarting blindly is unsafe
    "obs_overrun",       # the dsart watchdog already hard-stops this
    "sky_stale", "sky_no_data", "sefd_stale", "tool_error",
}

_RULES: dict[str, _Rule] = {
    "search_nodes_down": _bounce_search,
}


class RecoveryPlaybook:
    def __init__(self, rules: dict[str, _Rule] | None = None) -> None:
        self._rules = dict(rules if rules is not None else _RULES)

    def propose(self, report: HealthReport) -> list[RecoveryProposal]:
        """One proposal per recoverable finding code (deduped by code)."""
        out: list[RecoveryProposal] = []
        seen: set[str] = set()
        for f in report.findings:
            if f.code in seen or f.code in _NO_AUTO:
                continue
            rule = self._rules.get(f.code)
            if rule is None:
                continue
            seen.add(f.code)
            out.append(rule(f.details))
        return out


__all__ = ["RecoveryProposal", "RecoveryPlaybook"]
