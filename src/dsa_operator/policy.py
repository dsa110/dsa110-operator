"""Policy loader + gate engine — reads ``config/policy.yaml``.

The policy file is the single source of truth for *what the agent may do*.
Phase 0/1 used only its read-only surface. Phase 2 adds the **gate
engine**: resolving each control action to ``autonomous`` / ``approval`` /
``forbidden``, honouring the commissioning-vs-target split and per-action
promotions from ``config/local.yaml``.

Gate resolution
---------------
Each control action declares a conservative ``commissioning`` gate and a
steady-state ``target`` gate. The **active** gate is ``commissioning``
until the action is explicitly *promoted* (listed under ``promote:`` in
``config/local.yaml``), after which it becomes ``target``. A promotion can
only ever *loosen* toward the stated target; it can never make an action
looser than its target. Promotion itself is an audited event.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("dsa_operator.policy")

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_DEFAULT_POLICY_PATH = _CONFIG_DIR / "policy.yaml"
_DEFAULT_LOCAL_PATH = _CONFIG_DIR / "local.yaml"

# Gate vocabulary, ordered from loosest to strictest.
GATE_AUTONOMOUS = "autonomous"
GATE_APPROVAL = "approval"
GATE_FORBIDDEN = "forbidden"
_GATE_RANK = {GATE_AUTONOMOUS: 1, GATE_APPROVAL: 2, GATE_FORBIDDEN: 3}


def _stricter(a: str, b: str) -> str:
    """Return the more restrictive of two gates."""
    return a if _GATE_RANK.get(a, 99) >= _GATE_RANK.get(b, 99) else b


@dataclass(frozen=True)
class Policy:
    version: int
    mode: str
    paused: bool
    read_only: frozenset[str]
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    pointing: dict[str, Any] = field(default_factory=dict)
    autonomy: dict[str, Any] = field(default_factory=dict)
    observing: dict[str, Any] = field(default_factory=dict)
    approval_ttl_s: int = 300
    two_person: frozenset[str] = frozenset()
    promoted: frozenset[str] = frozenset()
    raw: dict[str, Any] = field(default_factory=dict)

    # -- queries --------------------------------------------------------------
    def is_read_only_action(self, action: str) -> bool:
        return action in self.read_only

    def is_control_action(self, action: str) -> bool:
        return action in self.actions

    def known_action(self, action: str) -> bool:
        return action in self.read_only or action in self.actions

    def gate_for(self, action: str) -> str:
        """Active gate for a control action.

        ``commissioning`` unless promoted, then ``target`` — but never
        looser than ``target`` (a promotion can't exceed the stated goal).
        Unknown actions are ``forbidden`` (fail closed).
        """
        spec = self.actions.get(action)
        if not spec:
            return GATE_FORBIDDEN
        commissioning = str(spec.get("commissioning", GATE_FORBIDDEN))
        target = str(spec.get("target", GATE_FORBIDDEN))
        if action in self.promoted:
            return target
        # Active = the stricter of commissioning and target until promoted.
        return _stricter(commissioning, target)

    def needs_two_person(self, action: str) -> bool:
        return action in self.two_person

    def required_approvers(self, action: str) -> int:
        return 2 if self.needs_two_person(action) else 1

    def is_reversible(self, action: str) -> bool:
        return bool(self.actions.get(action, {}).get("reversible", False))

    def action_note(self, action: str) -> str:
        return str(self.actions.get(action, {}).get("note", ""))


class PolicyConfigError(ValueError):
    """A policy / local-override config file is present but invalid.

    We fail **loud** with an actionable message rather than (a) letting an
    opaque ``yaml.ParserError`` traceback escape from deep in startup, or
    (b) silently falling back to an empty promote list — which in ``live``
    mode would turn *every* action into a dry-run, the exact silent failure
    that makes "the telescope never starts" so hard to diagnose.
    """


def _load_yaml_file(p: Path) -> Any:
    """``yaml.safe_load`` a file, re-raising parse errors as a clear,
    file-attributed :class:`PolicyConfigError`."""
    import yaml

    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PolicyConfigError(f"{p} is not valid YAML: {exc}") from exc


def load_promotions(path: Optional[str | Path] = None) -> frozenset[str]:
    """Read the optional ``config/local.yaml`` ``promote:`` list.

    A missing file means "nothing promoted" (the safe default). A file that
    exists but is malformed raises :class:`PolicyConfigError` — we never
    silently swallow it into an empty list.
    """
    p = Path(path) if path is not None else _DEFAULT_LOCAL_PATH
    if not p.exists():
        return frozenset()
    raw = _load_yaml_file(p)
    if not isinstance(raw, dict):
        raise PolicyConfigError(
            f"{p}: expected a YAML mapping with a 'promote:' key, got "
            f"{type(raw).__name__}."
        )
    promote = raw.get("promote", []) or []
    if isinstance(promote, str) or not isinstance(promote, (list, tuple, set)):
        raise PolicyConfigError(
            f"{p}: 'promote' must be a YAML block list of action names, e.g.\n"
            "  promote:\n    - point_array\n    - utc_start\n"
            "Do NOT write 'promote: []' on its own line above the items — that "
            "hard-codes an EMPTY list and orphans the items below it (invalid "
            f"YAML). Got: {promote!r}"
        )
    return frozenset(str(a).strip() for a in promote if str(a).strip())


def load_policy(
    path: Optional[str | Path] = None,
    *,
    local_path: Optional[str | Path] = None,
) -> Policy:
    p = Path(path) if path is not None else _DEFAULT_POLICY_PATH
    raw = _load_yaml_file(p)
    approval = raw.get("approval", {}) or {}
    actions = dict(raw.get("actions", {}) or {})
    promoted = load_promotions(local_path)

    # Catch typos like ``utc_strat`` early: a promoted name that is not a real
    # control action will silently never take effect, so warn loudly.
    unknown = sorted(promoted - set(actions))
    if unknown:
        LOG.warning(
            "config/local.yaml promotes unknown action(s) %s — not in the "
            "policy's actions, so they have NO effect (typo?).",
            ", ".join(unknown),
        )

    return Policy(
        version=int(raw.get("version", 0)),
        mode=str(raw.get("mode", "shadow")),
        paused=bool(raw.get("paused", False)),
        read_only=frozenset(raw.get("read_only", []) or []),
        actions=actions,
        pointing=dict(raw.get("pointing", {}) or {}),
        autonomy=dict(raw.get("autonomy", {}) or {}),
        observing=dict(raw.get("observing", {}) or {}),
        approval_ttl_s=int(approval.get("ttl_seconds", 300)),
        two_person=frozenset(approval.get("two_person", []) or []),
        promoted=promoted,
        raw=raw,
    )


__all__ = [
    "Policy",
    "PolicyConfigError",
    "load_policy",
    "load_promotions",
    "GATE_AUTONOMOUS",
    "GATE_APPROVAL",
    "GATE_FORBIDDEN",
]
