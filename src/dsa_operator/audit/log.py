"""Append-only, secret-redacted audit log (the durable system of record).

Every record is one JSON object on its own line (JSONL), flushed and
``fsync``-ed immediately so a crash can't lose the tail. Records are
append-only: there is no update/delete API. Files roll by UTC day.

Redaction runs on every record before it touches disk (or Slack, or the
model context): values under sensitive key names, and anything that looks
like a token/key, are replaced with ``"***REDACTED***"``. This keeps the
operator's promise that secrets never leave the box and never reach the
model.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("dsa_operator.audit")

REDACTED = "***REDACTED***"

# Key names whose values are always redacted (case-insensitive substring).
_SENSITIVE_KEY_PARTS = (
    "token", "secret", "password", "passwd", "api_key", "apikey",
    "authorization", "auth_header", "private_key", "client_secret",
    "bearer", "cookie", "session",
)

# Value patterns that look like credentials even under an innocent key.
_SENSITIVE_VALUE_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),          # Anthropic keys
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{8,}"),       # Slack tokens
    re.compile(r"ya29\.[A-Za-z0-9_\-]{8,}"),           # Google OAuth tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # PEM private keys
)


def _redact_str(s: str) -> str:
    for rx in _SENSITIVE_VALUE_RES:
        s = rx.sub(REDACTED, s)
    return s


def redact(obj: Any) -> Any:
    """Recursively redact secrets from a JSON-serialisable object."""
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and any(
                part in k.lower() for part in _SENSITIVE_KEY_PARTS
            ):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _redact_str(obj)
    return obj


@dataclass
class AuditRecord:
    """One audit event."""

    action: str                       # e.g. "get_fleet_status", "point_array"
    kind: str = "read"                # read | control | policy | approval | system
    actor: str = "system"            # local operator name, or "system"/"agent"
    ok: bool = True
    mode: str = "live"               # live | shadow
    params: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    note: str = ""
    ts: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "iso_ts": datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat(),
            "action": self.action,
            "kind": self.kind,
            "actor": self.actor,
            "ok": self.ok,
            "mode": self.mode,
            "params": redact(self.params),
            "result": redact(self.result),
            "note": self.note,
        }


class AuditLog:
    """Thread-safe append-only JSONL writer with daily rollover.

    Optionally mirrors a one-line human summary to a Slack notifier and a
    structured row to the shared etcd ``/mon/audit/...`` trail (Phase 2,
    via an injected ``etcd_sink`` callable). Sink failures never break the
    local write — auditability of the local record is paramount.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] = "audit_log",
        *,
        slack: Optional["SlackNotifierProto"] = None,
        etcd_sink: Optional[Any] = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._slack = slack
        self._etcd_sink = etcd_sink
        self._lock = threading.Lock()

    def _path_for(self, ts: float) -> Path:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
        return self.root / f"audit-{day}.jsonl"

    def record(self, rec: AuditRecord) -> AuditRecord:
        """Write one record. Returns it (for convenient chaining)."""
        payload = rec.to_json()
        line = json.dumps(payload, separators=(",", ":"), default=str)
        path = self._path_for(rec.ts)
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        # Best-effort fan-out; never let these break the durable write.
        if self._slack is not None:
            try:
                self._slack.notify_audit(payload)
            except Exception:                              # noqa: BLE001
                LOG.warning("slack audit notify failed", exc_info=True)
        if self._etcd_sink is not None:
            try:
                self._etcd_sink(payload)
            except Exception:                              # noqa: BLE001
                LOG.warning("etcd audit sink failed", exc_info=True)
        return rec

    # Convenience constructors -------------------------------------------------
    def read(self, action: str, *, actor: str = "system", ok: bool = True,
             params: Optional[dict[str, Any]] = None, note: str = "") -> AuditRecord:
        return self.record(AuditRecord(
            action=action, kind="read", actor=actor, ok=ok,
            params=params or {}, note=note,
        ))

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        """Return the most recent ``n`` records across day files."""
        files = sorted(self.root.glob("audit-*.jsonl"))
        out: list[dict[str, Any]] = []
        for path in reversed(files):
            lines = path.read_text(encoding="utf-8").splitlines()
            for ln in reversed(lines):
                if not ln.strip():
                    continue
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
                if len(out) >= n:
                    return list(reversed(out))
        return list(reversed(out))


class SlackNotifierProto:
    """Structural type for the Slack sink (avoids a hard import cycle)."""

    def notify_audit(self, payload: dict[str, Any]) -> None:  # pragma: no cover
        ...


__all__ = ["AuditLog", "AuditRecord", "redact", "REDACTED"]
