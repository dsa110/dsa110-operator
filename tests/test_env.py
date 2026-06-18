"""Secrets loader: file parsing, no-overwrite, name-only reporting."""
from __future__ import annotations

import os

from dsa_operator import env


def test_loads_from_explicit_file(tmp_path, monkeypatch):
    f = tmp_path / "secrets.env"
    f.write_text('ANTHROPIC_API_KEY="sk-ant-xyz"\n# a comment\nFOO=bar\n')
    monkeypatch.setenv("DSA_OPERATOR_SECRETS", str(f))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FOO", raising=False)
    loaded = env.load_secrets()
    assert "ANTHROPIC_API_KEY" in loaded and "FOO" in loaded
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-xyz"
    assert os.environ["FOO"] == "bar"


def test_never_overwrites_existing_env(tmp_path, monkeypatch):
    f = tmp_path / "secrets.env"
    f.write_text("ANTHROPIC_API_KEY=from-file\n")
    monkeypatch.setenv("DSA_OPERATOR_SECRETS", str(f))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    loaded = env.load_secrets()
    assert "ANTHROPIC_API_KEY" not in loaded          # already set, untouched
    assert os.environ["ANTHROPIC_API_KEY"] == "from-env"


def test_have_anthropic_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert env.have_anthropic_key() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert env.have_anthropic_key() is True


def test_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("DSA_OPERATOR_SECRETS", str(tmp_path / "nope.env"))
    # don't assert empty (other default files may exist on the box); just no crash
    env.load_secrets()
