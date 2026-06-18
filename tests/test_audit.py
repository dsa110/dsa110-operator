"""Audit log: redaction, append-only JSONL, daily file, tail, Slack fan-out."""
import json

from dsa_operator.audit.log import AuditLog, AuditRecord, redact
from dsa_operator.audit.slack import SlackNotifier, format_audit_line


def test_redact_sensitive_keys_and_values():
    obj = {
        "api_key": "sk-ant-abcdef123456",
        "nested": {"password": "hunter2", "ok": "fine"},
        "blob": "token here sk-ant-zzzzzzzz9999 trailing",
        "list": [{"secret": "x"}, "plain"],
    }
    red = redact(obj)
    assert red["api_key"] == "***REDACTED***"
    assert red["nested"]["password"] == "***REDACTED***"
    assert red["nested"]["ok"] == "fine"
    assert "sk-ant-" not in red["blob"]
    assert red["list"][0]["secret"] == "***REDACTED***"
    assert red["list"][1] == "plain"


def test_record_writes_jsonl_and_redacts(tmp_path):
    log = AuditLog(tmp_path)
    log.record(AuditRecord(
        action="get_mon", kind="read", actor="alice@dsa",
        params={"authorization": "Bearer xoxb-123456789"},
    ))
    files = list(tmp_path.glob("audit-*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text().strip()
    rec = json.loads(line)
    assert rec["action"] == "get_mon"
    assert rec["actor"] == "alice@dsa"
    assert rec["params"]["authorization"] == "***REDACTED***"
    assert "iso_ts" in rec


def test_tail_returns_recent_in_order(tmp_path):
    log = AuditLog(tmp_path)
    for i in range(5):
        log.read(f"act{i}", actor="bob")
    tail = log.tail(3)
    assert [r["action"] for r in tail] == ["act2", "act3", "act4"]


def test_slack_disabled_is_noop_and_filters_reads():
    n = SlackNotifier(webhook_url=None)
    assert not n.enabled
    # No exception even though there's no webhook.
    n.notify_audit({"action": "get_mon", "kind": "read", "ok": True})


def test_slack_rejects_non_allowlisted_host():
    import pytest

    with pytest.raises(ValueError):
        SlackNotifier(webhook_url="https://evil.example.com/x")


def test_slack_fanout_posts_control_events(tmp_path):
    posted = []

    class Spy(SlackNotifier):
        def __init__(self):
            super().__init__(webhook_url="https://hooks.slack.com/services/T/B/X")

        def post(self, text):
            posted.append(text)

    log = AuditLog(tmp_path, slack=Spy())
    log.record(AuditRecord(action="point_array", kind="control", actor="carol",
                           mode="shadow", note="dec=16"))
    log.read("get_mon", actor="carol")          # read -> not posted by default
    assert len(posted) == 1
    assert "point_array" in posted[0]
    assert "[SHADOW]" in posted[0]


def test_format_audit_line_marks_failure():
    line = format_audit_line({"action": "x", "kind": "control", "ok": False,
                              "actor": "z", "mode": "live"})
    assert "FAILED" in line
