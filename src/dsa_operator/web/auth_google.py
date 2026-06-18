"""Google OAuth2 (authorization-code flow) — done with ``requests``.

No Authlib dependency: we only need the standard three calls (authorize →
token → userinfo), and doing them explicitly keeps the egress surface
obvious (only ``accounts.google.com`` / ``oauth2.googleapis.com`` /
``www.googleapis.com``, all on the allowlist).

Authorization is an allowlist: an email is accepted only if it is in
``allowed_emails`` or its domain is in ``allowed_domains``. Everyone who
passes is a *monitoring* user; the single-executor lease (Phase 2) decides
who may additionally control.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Protocol
from urllib.parse import urlencode

LOG = logging.getLogger("dsa_operator.web.auth")

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"
_SCOPES = "openid email profile"


class AuthProvider(Protocol):
    def authorize_url(self, state: str) -> str: ...
    def exchange_code(self, code: str) -> str: ...        # returns email
    def is_authorized(self, email: str) -> bool: ...


@dataclass
class GoogleAuth:
    client_id: str
    client_secret: str
    redirect_uri: str
    allowed_domains: frozenset[str] = field(default_factory=frozenset)
    allowed_emails: frozenset[str] = field(default_factory=frozenset)
    timeout_s: float = 8.0

    @classmethod
    def from_env(cls) -> "GoogleAuth":
        domains = {
            d.strip().lower()
            for d in os.environ.get("DSA_OPERATOR_ALLOWED_DOMAINS", "").split(",")
            if d.strip()
        }
        emails = {
            e.strip().lower()
            for e in os.environ.get("DSA_OPERATOR_ALLOWED_EMAILS", "").split(",")
            if e.strip()
        }
        return cls(
            client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
            client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            redirect_uri=os.environ.get(
                "DSA_OPERATOR_REDIRECT_URI",
                "http://localhost:8787/auth/callback",
            ),
            allowed_domains=frozenset(domains),
            allowed_emails=frozenset(emails),
        )

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def authorize_url(self, state: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": _SCOPES,
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"{_AUTH_ENDPOINT}?{urlencode(params)}"

    def exchange_code(self, code: str) -> str:
        import requests  # lazy

        tok = requests.post(
            _TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=self.timeout_s,
        )
        tok.raise_for_status()
        access_token = tok.json()["access_token"]
        info = requests.get(
            _USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=self.timeout_s,
        )
        info.raise_for_status()
        data = info.json()
        if not data.get("email_verified", False):
            raise PermissionError("Google account email is not verified")
        return str(data["email"]).lower()

    def is_authorized(self, email: str) -> bool:
        email = email.lower()
        if email in self.allowed_emails:
            return True
        domain = email.split("@")[-1] if "@" in email else ""
        return domain in self.allowed_domains


@dataclass
class FakeAuth:
    """Test/dev auth: returns a fixed email; authorizes everyone."""

    email: str = "tester@dsa110.org"
    redirect_uri: str = "http://localhost:8787/auth/callback"

    def authorize_url(self, state: str) -> str:
        return f"{self.redirect_uri}?code=fake&state={state}"

    def exchange_code(self, code: str) -> str:
        return self.email

    def is_authorized(self, email: str) -> bool:
        return True


__all__ = ["AuthProvider", "GoogleAuth", "FakeAuth"]
