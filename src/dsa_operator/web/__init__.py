"""Operator web console (Phase 1).

Flask app + Google SSO + the multi-user read-only monitoring/chat surface.
The single-executor lease UI and approval flow arrive in later phases.
"""
from __future__ import annotations

from dsa_operator.web.app import create_app

__all__ = ["create_app"]
