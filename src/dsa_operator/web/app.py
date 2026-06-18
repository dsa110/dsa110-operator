"""Operator web console (Phase 1) — Flask, Google SSO, multi-user monitoring.

Read-only by design: every API endpoint serves a read-only tool, scoped to
the logged-in Google identity (which is stamped into every audit row), and
the chat endpoint routes to the agent, which itself holds only the
read-only tool surface. There are **no control routes** in this phase.

The app factory takes injectable ``auth`` / ``tools_factory`` / ``agent`` /
``audit`` so tests run with fakes (no Google, no network, no live etcd).
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Callable, Optional

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from dsa_operator import DEFAULT_LOCAL_DASHBOARD_PORT, DEFAULT_LOCAL_ETCD_PORT
from dsa_operator.agent import build_default_agent
from dsa_operator.agent.base import Agent
from dsa_operator.audit.log import AuditLog, AuditRecord
from dsa_operator.tools.readonly import ReadOnlyTools, ToolError
from dsa_operator.web.auth_google import AuthProvider, GoogleAuth

LOG = logging.getLogger("dsa_operator.web")

ToolsFactory = Callable[[str], ReadOnlyTools]


def _default_tools_factory(audit: AuditLog) -> ToolsFactory:
    """Build a real ReadOnlyTools per request, actor = the logged-in user."""
    from dsa_operator.dashboard import DashboardClient
    from dsa_operator.etcd.read import connect_readonly

    etcd = connect_readonly(port=int(os.environ.get("DSA_OPERATOR_ETCD_PORT",
                                                     DEFAULT_LOCAL_ETCD_PORT)))
    dash = DashboardClient(port=int(os.environ.get("DSA_OPERATOR_DASHBOARD_PORT",
                                                   DEFAULT_LOCAL_DASHBOARD_PORT)))

    def factory(actor: str) -> ReadOnlyTools:
        return ReadOnlyTools(etcd, dash, audit, actor=actor)

    return factory


def create_app(
    *,
    auth: Optional[AuthProvider] = None,
    tools_factory: Optional[ToolsFactory] = None,
    agent: Optional[Agent] = None,
    audit: Optional[AuditLog] = None,
    secret_key: Optional[str] = None,
) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.secret_key = (
        secret_key
        or os.environ.get("DSA_OPERATOR_SECRET_KEY")
        or secrets.token_hex(32)
    )
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    audit = audit or AuditLog(os.environ.get("DSA_OPERATOR_AUDIT_ROOT", "audit_log"))
    auth = auth or GoogleAuth.from_env()
    tools_factory = tools_factory or _default_tools_factory(audit)
    agent = agent or build_default_agent()

    # -- auth helpers ---------------------------------------------------------
    def current_user() -> Optional[str]:
        return session.get("user")

    def require_user() -> str:
        user = current_user()
        if not user:
            abort(401)
        return user

    def _tools_for_request() -> ReadOnlyTools:
        return tools_factory(require_user())

    # -- auth routes ----------------------------------------------------------
    @app.route("/login")
    def login():
        state = secrets.token_urlsafe(16)
        session["oauth_state"] = state
        return redirect(auth.authorize_url(state))

    @app.route("/auth/callback")
    def auth_callback():
        if "error" in request.args:
            return _login_error(request.args.get("error", "oauth error"))
        state = request.args.get("state")
        if not state or state != session.pop("oauth_state", None):
            return _login_error("bad oauth state")
        code = request.args.get("code")
        if not code:
            return _login_error("missing code")
        try:
            email = auth.exchange_code(code)
        except Exception as exc:                           # noqa: BLE001
            LOG.warning("oauth exchange failed: %s", exc)
            return _login_error("login failed")
        if not auth.is_authorized(email):
            audit.record(AuditRecord(action="login_denied", kind="system",
                                     actor=email, ok=False,
                                     note="not on operator allowlist"))
            return _login_error(f"{email} is not authorized", code=403)
        session["user"] = email
        audit.record(AuditRecord(action="login", kind="system", actor=email,
                                 note="google sso"))
        return redirect(url_for("index"))

    @app.route("/logout", methods=["POST", "GET"])
    def logout():
        user = current_user()
        session.clear()
        if user:
            audit.record(AuditRecord(action="logout", kind="system", actor=user))
        return redirect(url_for("index"))

    def _login_error(msg: str, code: int = 401):
        return render_template("login.html", error=msg), code

    # -- pages ----------------------------------------------------------------
    @app.route("/")
    def index():
        user = current_user()
        if not user:
            return render_template("login.html", error=None)
        return render_template("console.html", user=user, agent_model=getattr(agent, "model", "?"))

    @app.route("/api/whoami")
    def whoami():
        return jsonify({"user": require_user()})

    # -- read-only API --------------------------------------------------------
    def _ro(method: str):
        def handler():
            tools = _tools_for_request()
            try:
                return jsonify({"ok": True, "data": getattr(tools, method)()})
            except ToolError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            except Exception as exc:                       # noqa: BLE001
                LOG.exception("%s failed", method)
                return jsonify({"ok": False, "error": str(exc)}), 502
        handler.__name__ = f"api_{method}"
        return handler

    app.add_url_rule("/api/fleet", view_func=_ro("get_fleet_status"))
    app.add_url_rule("/api/pointing", view_func=_ro("get_array_pointing"))
    app.add_url_rule("/api/sky", view_func=_ro("get_sky_status"))
    app.add_url_rule("/api/rfi", view_func=_ro("get_rfi_summary"))
    app.add_url_rule("/api/sefd", view_func=_ro("get_sefd"))
    app.add_url_rule("/api/injections", view_func=_ro("query_injections"))
    app.add_url_rule("/api/candidates", view_func=_ro("list_candidates"))

    @app.route("/api/audit")
    def api_audit():
        tools = _tools_for_request()
        n = request.args.get("n", default=50, type=int)
        return jsonify({"ok": True, "data": tools.get_audit_log(n)})

    @app.route("/api/mon")
    def api_mon():
        tools = _tools_for_request()
        key = request.args.get("key", "")
        try:
            return jsonify({"ok": True, "data": tools.get_mon(key)})
        except ToolError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    # -- chat -----------------------------------------------------------------
    @app.route("/api/chat", methods=["POST"])
    def api_chat():
        user = require_user()
        body = request.get_json(silent=True) or {}
        message = (body.get("message") or "").strip()
        if not message:
            return jsonify({"ok": False, "error": "empty message"}), 400
        tools = tools_factory(user)
        audit.record(AuditRecord(action="chat", kind="read", actor=user,
                                 params={"message": message}))
        try:
            resp = agent.chat(message, actor=user, tools=tools)
        except Exception as exc:                           # noqa: BLE001
            LOG.exception("agent chat failed")
            return jsonify({"ok": False, "error": str(exc)}), 502
        return jsonify({
            "ok": True,
            "text": resp.text,
            "model": resp.model,
            "tool_calls": [
                {"name": c.name, "args": c.args, "ok": c.ok, "error": c.error}
                for c in resp.tool_calls
            ],
        })

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "phase": 1, "authed": bool(current_user())})

    return app


def main() -> int:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app()
    host = os.environ.get("DSA_OPERATOR_BIND", "127.0.0.1")
    port = int(os.environ.get("DSA_OPERATOR_PORT", "8787"))
    app.run(host=host, port=port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
