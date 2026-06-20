"""Local operator identity resolution (replaces Google SSO)."""
from dsa_operator.web.identity import resolve_operator


def test_explicit_wins(monkeypatch):
    monkeypatch.setenv("DSA_OPERATOR_USER", "envuser")
    assert resolve_operator("alice") == "alice"


def test_env_user(monkeypatch):
    monkeypatch.setenv("DSA_OPERATOR_USER", "casey")
    assert resolve_operator() == "casey"


def test_falls_back_to_os_user(monkeypatch):
    monkeypatch.delenv("DSA_OPERATOR_USER", raising=False)
    monkeypatch.setattr("getpass.getuser", lambda: "osuser")
    assert resolve_operator() == "osuser"


def test_sanitises_and_defaults(monkeypatch):
    monkeypatch.delenv("DSA_OPERATOR_USER", raising=False)
    assert resolve_operator("  weird name!!/;") == "weirdname"
    monkeypatch.setattr("getpass.getuser", lambda: "!!!")
    assert resolve_operator() == "operator"
