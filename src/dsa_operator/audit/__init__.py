"""Audit + notification.

"Log everything": every tool call (read or, later, control), policy
decision, approval, and result is recorded to an append-only local JSONL
log (the durable system of record), and human-facing summaries are pushed
to Slack. Secrets are redacted before anything is written or sent.
"""
from __future__ import annotations

from dsa_operator.audit.log import AuditLog, AuditRecord, redact
from dsa_operator.audit.slack import SlackNotifier

__all__ = ["AuditLog", "AuditRecord", "redact", "SlackNotifier"]
