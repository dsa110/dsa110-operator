"""Policy loader — reads ``config/policy.yaml`` (the capability document).

Phase 0 uses only the read-only surface of the policy: the set of
allow-listed read actions and the global ``paused`` / ``mode`` flags. The
full gate engine (resolving ``autonomous`` / ``approval`` / ``forbidden``,
approvals, two-person, commissioning vs target) lands with the control
surface in Phase 2, on top of this same file.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("dsa_operator.policy")

_DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "policy.yaml"
)


@dataclass(frozen=True)
class Policy:
    version: int
    mode: str
    paused: bool
    read_only: frozenset[str]
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    pointing: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def is_read_only_action(self, action: str) -> bool:
        return action in self.read_only

    def known_action(self, action: str) -> bool:
        return action in self.read_only or action in self.actions


def load_policy(path: Optional[str | Path] = None) -> Policy:
    import yaml

    p = Path(path) if path is not None else _DEFAULT_POLICY_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Policy(
        version=int(raw.get("version", 0)),
        mode=str(raw.get("mode", "shadow")),
        paused=bool(raw.get("paused", False)),
        read_only=frozenset(raw.get("read_only", []) or []),
        actions=dict(raw.get("actions", {}) or {}),
        pointing=dict(raw.get("pointing", {}) or {}),
        raw=raw,
    )


__all__ = ["Policy", "load_policy"]
