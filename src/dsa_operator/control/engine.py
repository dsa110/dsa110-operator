"""The control gate engine.

Every control attempt runs the same gauntlet, in order:

1. **Known action?**     unknown verbs fail closed.
2. **Paused (e-stop)?**  if globally paused, all control fails closed.
3. **Executor lease?**   only the session holding the lease may proceed.
4. **Gate?**             ``forbidden`` → deny; ``approval`` → require a
                         matching, unexpired approval grant; ``autonomous``
                         → proceed.
5. **Valid params?**     the verb validates (e.g. the pointing envelope).
6. **Execute or shadow?** in this build there is **no live executor**, so
                         the result is always a rendered *plan*, audited as
                         a dry run. Phase 3 supplies a live executor and the
                         same gauntlet guards it.

This ordering means a buggy or compromised caller cannot do anything: with
no live executor injected, :class:`ControlEngine` cannot mutate observatory
state even if the policy says ``mode: live``.
"""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from dsa_operator.audit.log import AuditLog, AuditRecord
from dsa_operator.control.approvals import ApprovalStore
from dsa_operator.control.lease import ExecutorLease
from dsa_operator.policy import (
    GATE_APPROVAL,
    GATE_AUTONOMOUS,
    GATE_FORBIDDEN,
    Policy,
)
from dsa_operator.control.verbs import VerbError, get_verb

LOG = logging.getLogger("dsa_operator.control.engine")

PAUSE_KEY = "/operator/control/paused"


class Outcome(str, enum.Enum):
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"
    SHADOW = "shadow"
    EXECUTED = "executed"


@dataclass
class Decision:
    outcome: Outcome
    action: str
    actor: str
    gate: str = ""
    mode: str = "shadow"
    reason: str = ""
    plan: Optional[dict[str, Any]] = None
    approval: Optional[dict[str, Any]] = None
    holder: Optional[dict[str, Any]] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.outcome in (Outcome.SHADOW, Outcome.EXECUTED)

    def to_json(self) -> dict[str, Any]:
        d = {
            "outcome": self.outcome.value,
            "action": self.action,
            "actor": self.actor,
            "gate": self.gate,
            "mode": self.mode,
            "reason": self.reason,
        }
        for k in ("plan", "approval", "holder"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.extra:
            d.update(self.extra)
        return d


class ControlEngine:
    def __init__(
        self,
        policy: Policy,
        lease: ExecutorLease,
        approvals: ApprovalStore,
        audit: AuditLog,
        *,
        writer: Optional[Any] = None,     # OperatorEtcdWriter, for the pause key
        live_executor: Optional[Any] = None,
        now=time.time,
    ) -> None:
        self.policy = policy
        self.lease = lease
        self.approvals = approvals
        self.audit = audit
        self._writer = writer
        self._live = live_executor        # None ⇒ live impossible in this build
        self._now = now

    # -- e-stop ---------------------------------------------------------------
    def is_paused(self) -> bool:
        if self.policy.paused:
            return True
        if self._writer is None:
            return False
        got = self._writer.get(PAUSE_KEY)
        if not got:
            return False
        value, _ = got
        return bool(value.get("paused")) if isinstance(value, dict) else bool(value)

    def pause(self, actor: str, reason: str = "") -> bool:
        """Engage the e-stop. Any authenticated user may pause (fail-safe)."""
        if self._writer is None:
            return False
        self._writer.put(PAUSE_KEY, {"paused": True, "by": actor,
                                     "reason": reason, "ts": self._now()})
        self.audit.record(AuditRecord(action="pause", kind="control",
                                      actor=actor, mode="live",
                                      note=reason or "e-stop engaged"))
        return True

    def resume(self, actor: str) -> bool:
        """Clear the e-stop. Restricted to the lease holder upstream."""
        if self._writer is None:
            return False
        self._writer.put(PAUSE_KEY, {"paused": False, "by": actor,
                                     "ts": self._now()})
        self.audit.record(AuditRecord(action="resume", kind="control",
                                      actor=actor, mode="live"))
        return True

    # -- the gauntlet ---------------------------------------------------------
    def evaluate(
        self, action: str, params: Optional[dict[str, Any]] = None, *,
        actor: str, session_id: str,
    ) -> Decision:
        params = dict(params or {})

        # 1. known control action?
        if not self.policy.is_control_action(action):
            return self._deny(action, actor, "unknown_action",
                              f"{action!r} is not a known control action",
                              params)

        # 2. paused?
        if self.is_paused():
            return self._deny(action, actor, "paused",
                              "system is paused (e-stop engaged)", params)

        # 3. executor lease?
        holder = self.lease.holder()
        if holder is None or holder.session_id != session_id:
            d = self._deny(action, actor, "not_executor",
                           "you do not hold the executor lease", params)
            d.holder = holder.to_json() if holder else None
            return d

        gate = self.policy.gate_for(action)

        # 4. gate
        if gate == GATE_FORBIDDEN:
            return self._deny(action, actor, "forbidden",
                              f"{action!r} is forbidden by policy", params,
                              gate=gate)

        approval_used = None
        if gate == GATE_APPROVAL:
            approval_used = self.approvals.find_active(action, params)
            if approval_used is None:
                return self._needs_approval(action, actor, params, gate)

        # 5. params
        verb = get_verb(action)
        if verb is None:
            return self._deny(action, actor, "no_verb",
                              f"no verb implements {action!r}", params, gate=gate)
        try:
            plan = verb.plan(params, self.policy)
        except VerbError as exc:
            return self._deny(action, actor, "invalid_params", str(exc),
                              params, gate=gate)

        # 6. execute or shadow.  Live requires ALL of: an injected executor,
        #    policy mode=live, AND this action explicitly promoted. So the
        #    safe default (no executor / shadow / nothing promoted) can never
        #    mutate state, and graduation is strictly per-action.
        if self._should_execute_live(action):
            return self._execute_live(action, actor, params, gate, plan,
                                      approval_used)

        note = "dry run (shadow)"
        if self.policy.mode == "live" and self._live is None:
            note = "policy mode=live but no live executor in this build; shadow only"
        elif self.policy.mode == "live" and action not in self.policy.promoted:
            note = f"policy mode=live but {action!r} is not promoted; shadow only"
        elif self.policy.mode != "live":
            note = "policy mode=shadow; dry run"
        self.audit.record(AuditRecord(
            action=action, kind="control", actor=actor, ok=True, mode="shadow",
            params=params, result={"plan": plan.summary,
                                    "n_steps": len(plan.steps)},
            note=note,
        ))
        if approval_used is not None:
            self.approvals.consume(approval_used.id)
        d = Decision(Outcome.SHADOW, action, actor, gate=gate, mode="shadow",
                     reason=note, plan=plan.to_json(), holder=holder.to_json())
        if approval_used is not None:
            d.approval = approval_used.to_json(self._now())
        return d

    # -- helpers --------------------------------------------------------------
    def _deny(self, action: str, actor: str, reason: str, msg: str,
              params: dict[str, Any], *, gate: str = "") -> Decision:
        self.audit.record(AuditRecord(
            action=action, kind="control", actor=actor, ok=False,
            mode=self.policy.mode, params=params, note=f"denied: {reason}",
        ))
        return Decision(Outcome.DENIED, action, actor, gate=gate,
                        mode=self.policy.mode, reason=msg)

    def _needs_approval(self, action: str, actor: str, params: dict[str, Any],
                        gate: str) -> Decision:
        n = self.policy.required_approvers(action)
        two = self.policy.needs_two_person(action)
        self.audit.record(AuditRecord(
            action=action, kind="control", actor=actor, ok=False,
            mode=self.policy.mode, params=params,
            note=f"needs_approval ({n} approver(s)" + (", two-person)" if two else ")"),
        ))
        return Decision(
            Outcome.NEEDS_APPROVAL, action, actor, gate=gate,
            mode=self.policy.mode,
            reason=f"requires {n} approver(s)" + (" (two-person)" if two else ""),
            extra={"required_approvers": n, "two_person": two},
        )

    def _should_execute_live(self, action: str) -> bool:
        return (
            self._live is not None
            and self.policy.mode == "live"
            and action in self.policy.promoted
        )

    def _execute_live(self, action, actor, params, gate, plan,
                      approval_used) -> Decision:
        # Hand the plan to the live executor. Reached only when the action is
        # promoted AND mode=live AND an executor is wired (see
        # _should_execute_live). On failure we audit and surface the error
        # rather than pretend success.
        try:
            result = self._live.execute(plan, actor=actor)
        except Exception as exc:                            # noqa: BLE001
            self.audit.record(AuditRecord(
                action=action, kind="control", actor=actor, ok=False,
                mode="live", params=params, note=f"execute failed: {exc}",
            ))
            return Decision(Outcome.DENIED, action, actor, gate=gate,
                            mode="live", reason=f"execution failed: {exc}",
                            plan=plan.to_json())
        self.audit.record(AuditRecord(
            action=action, kind="control", actor=actor, ok=True, mode="live",
            params=params, result={"plan": plan.summary}, note="executed",
        ))
        if approval_used is not None:
            self.approvals.consume(approval_used.id)
        return Decision(Outcome.EXECUTED, action, actor, gate=gate, mode="live",
                        reason="executed", plan=plan.to_json(),
                        extra={"result": result})


__all__ = ["ControlEngine", "Decision", "Outcome", "PAUSE_KEY"]
