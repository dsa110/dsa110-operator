"""Local operator identity (replaces Google SSO).

The operator console runs on *your own laptop*, bound to loopback and reached
only through the SSH tunnel to h23. Anyone who can open ``localhost:8787`` is
already the person sitting at that laptop, so there is nothing to authenticate
against — a login screen would add friction with no security benefit.

We still want a *name*, though: every action is audited and the single-executor
lease shows who is in charge, so when several people each run their own laptop
instance and contend for control on h23 the audit trail and lease should read
``vikram`` / ``casey`` rather than a shared placeholder.

So identity here is just a label, resolved once per process:
  1. ``$DSA_OPERATOR_USER`` if set,
  2. otherwise the OS login name,
  3. otherwise ``"operator"``.

Authorisation is unchanged and lives elsewhere: the etcd lease decides who may
*execute*, and the dsa110-rt dashboard can still lock agents out or pin the
executor (see :mod:`dsa_operator.control.authority`).
"""
from __future__ import annotations

import getpass
import os
import re

_SANITISE = re.compile(r"[^A-Za-z0-9_.@+-]")


def _clean(name: str) -> str:
    name = _SANITISE.sub("", (name or "").strip())
    return name[:64] or "operator"


def resolve_operator(explicit: str | None = None) -> str:
    """The local operator's name. ``explicit`` (e.g. a test/CLI override) wins,
    then ``$DSA_OPERATOR_USER``, then the OS user, then ``operator``."""
    if explicit:
        return _clean(explicit)
    env = os.environ.get("DSA_OPERATOR_USER")
    if env:
        return _clean(env)
    try:
        return _clean(getpass.getuser())
    except Exception:                                  # noqa: BLE001
        return "operator"


__all__ = ["resolve_operator"]
