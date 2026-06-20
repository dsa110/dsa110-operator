"""Operator web console.

Flask app for the local, laptop-only monitoring/chat/control surface. Identity
is the local operator name (no SSO — see :mod:`dsa_operator.web.identity`); the
single-executor lease arbitrates who may control.
"""
from __future__ import annotations

from dsa_operator.web.app import create_app

__all__ = ["create_app"]
