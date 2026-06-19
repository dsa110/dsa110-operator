"""Slack notifier — the human-facing audit + alert channel.

Posts compact one-line summaries to a Slack incoming-webhook URL. This is
deliberately best-effort and side-channel: the durable record is the local
JSONL log (:mod:`dsa_operator.audit.log`); Slack is for humans to watch.

The webhook URL is a secret — pass it in from the environment
(``DSA_OPERATOR_SLACK_WEBHOOK``); never commit it. If unset, the notifier
is a no-op so dev/test never needs network.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

LOG = logging.getLogger("dsa_operator.audit.slack")

# Only this host is permitted for Slack egress (see config/egress_allowlist.yaml).
_SLACK_HOST = "hooks.slack.com"


def _emoji(payload: dict[str, Any]) -> str:
    if not payload.get("ok", True):
        return ":red_circle:"
    if payload.get("kind") == "control":
        return ":large_blue_circle:" if payload.get("mode") == "live" else ":white_circle:"
    return ":small_blue_diamond:"


def format_audit_line(payload: dict[str, Any]) -> str:
    """One-line human summary of an audit record."""
    mode = payload.get("mode", "live")
    mode_tag = "" if mode == "live" else f" [{mode.upper()}]"
    actor = payload.get("actor", "system")
    action = payload.get("action", "?")
    note = payload.get("note", "")
    suffix = f" — {note}" if note else ""
    return (
        f"{_emoji(payload)} *{action}*{mode_tag} "
        f"by `{actor}` ({payload.get('kind', 'read')})"
        f"{'' if payload.get('ok', True) else ' FAILED'}{suffix}"
    )


class SlackNotifier:
    """Best-effort Slack webhook poster.

    ``min_kind`` filters chatter: by default only control/policy/approval/
    system events and failures are posted (reads stay in the local log).
    """

    _POSTWORTHY_KINDS = {"control", "policy", "approval", "system"}

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        *,
        timeout_s: float = 4.0,
        post_reads: bool = False,
    ) -> None:
        self.webhook_url = (
            webhook_url
            or os.environ.get("DSA_OPERATOR_SLACK_WEBHOOK_URL")
            or os.environ.get("DSA_OPERATOR_SLACK_WEBHOOK")
        )
        self.timeout_s = timeout_s
        self.post_reads = post_reads
        if self.webhook_url and _SLACK_HOST not in self.webhook_url:
            raise ValueError(
                f"Slack webhook host not on egress allowlist (expected {_SLACK_HOST})"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def _should_post(self, payload: dict[str, Any]) -> bool:
        if not payload.get("ok", True):
            return True
        if payload.get("kind") in self._POSTWORTHY_KINDS:
            return True
        return self.post_reads

    def notify_audit(self, payload: dict[str, Any]) -> None:
        if not self.enabled or not self._should_post(payload):
            return
        self.post(format_audit_line(payload))

    def post(self, text: str) -> None:
        if not self.enabled:
            LOG.debug("slack disabled; would post: %s", text)
            return
        import requests  # lazy

        from dsa_operator.audit.egress import EgressError, assert_url_allowed
        try:
            assert_url_allowed(self.webhook_url)
        except EgressError:
            LOG.error("slack webhook host not on egress allowlist; refusing")
            return
        try:
            requests.post(self.webhook_url, json={"text": text}, timeout=self.timeout_s)
        except Exception:                                  # noqa: BLE001
            LOG.warning("slack post failed", exc_info=True)


def _main(argv: Optional[list] = None) -> int:  # pragma: no cover
    """`python -m dsa_operator.audit.slack --test "msg"` — post a test line."""
    import argparse

    from dsa_operator.env import load_secrets
    logging.basicConfig(level=logging.INFO)
    load_secrets()
    ap = argparse.ArgumentParser(description="Slack notifier self-test")
    ap.add_argument("--test", default="dsa110-operator slack test ✅",
                    help="message to post")
    args = ap.parse_args(argv)
    n = SlackNotifier()
    if not n.enabled:
        print("DSA_OPERATOR_SLACK_WEBHOOK is not set; nothing to post.")
        return 1
    n.post(args.test)
    print("posted (best-effort) to Slack webhook.")
    return 0


__all__ = ["SlackNotifier", "format_audit_line"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
