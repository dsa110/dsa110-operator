"""The typed tool surface the agent is allowed to call.

Phase 0 ships only :class:`ReadOnlyTools`. The model never sees a raw
etcd client, a raw HTTP client, or a shell — only these named,
input-validated methods, each of which is checked against the policy's
read-only allowlist and audited. Control tools (in shadow first) arrive
in later phases as a separate, lease- and policy-gated module.
"""
from __future__ import annotations

from dsa_operator.tools.readonly import ReadOnlyTools

__all__ = ["ReadOnlyTools"]
