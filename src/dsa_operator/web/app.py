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
import time
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
from dsa_operator.control.approvals import ApprovalError, ApprovalStore
from dsa_operator.control.engine import ControlEngine
from dsa_operator.control.lease import ExecutorLease, new_session_id
from dsa_operator.monitor.injection import InjectionHealthCheck
from dsa_operator.monitor.supervisor import AutonomyConfig, AutonomySupervisor
from dsa_operator.observing import astro
from dsa_operator.observing.plan import ObservingPlan, PlanError, PlanStore
from dsa_operator.observing.runner import PlanRunner
from dsa_operator.policy import Policy, load_policy
from dsa_operator.tools.readonly import ReadOnlyTools, ToolError
from dsa_operator.web.auth_google import AuthProvider, GoogleAuth

LOG = logging.getLogger("dsa_operator.web")

ToolsFactory = Callable[[str], ReadOnlyTools]


def _default_audit() -> AuditLog:
    """AuditLog with a Slack notifier wired from the environment (no-op if
    DSA_OPERATOR_SLACK_WEBHOOK is unset)."""
    from dsa_operator.audit.slack import SlackNotifier
    root = os.environ.get("DSA_OPERATOR_AUDIT_ROOT", "audit_log")
    try:
        slack = SlackNotifier()
    except ValueError:
        LOG.warning("slack webhook rejected by egress allowlist; disabling")
        slack = None
    return AuditLog(root, slack=slack)


def _default_control_engine(audit: AuditLog) -> ControlEngine:
    """Build the real control engine over the forwarded etcd + dashboard.

    A live executor IS wired here, but the engine fires it only when the
    policy is ``mode: live`` AND the specific action is promoted in
    ``config/local.yaml`` (see ``ControlEngine._should_execute_live``). With
    the shipped defaults (shadow, nothing promoted) every control path is a
    dry run — the live executor stays dormant.
    """
    from dsa_operator.audit.etcd_sink import EtcdAuditSink
    from dsa_operator.control.executors import (
        ControlEtcdWriter,
        DashboardControlClient,
        LiveExecutor,
    )
    from dsa_operator.etcd.read import connect_readonly
    from dsa_operator.etcd.write import connect_writer

    etcd_port = int(os.environ.get("DSA_OPERATOR_ETCD_PORT", DEFAULT_LOCAL_ETCD_PORT))
    dash_port = int(os.environ.get("DSA_OPERATOR_DASHBOARD_PORT",
                                   DEFAULT_LOCAL_DASHBOARD_PORT))
    writer = connect_writer(port=etcd_port)
    # Mirror audit rows into etcd's /operator/audit trail too.
    audit._etcd_sink = audit._etcd_sink or EtcdAuditSink(writer)  # type: ignore[attr-defined]

    read = connect_readonly(port=etcd_port)
    executor = LiveExecutor(
        dashboard=DashboardControlClient(port=dash_port),
        control_etcd=ControlEtcdWriter("127.0.0.1", etcd_port),
        read_etcd=read,
    )
    return ControlEngine(
        load_policy(), ExecutorLease(writer), ApprovalStore(), audit,
        writer=writer, read_etcd=read, live_executor=executor,
    )


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
    control: Optional[ControlEngine] = None,
    plan_store: Optional["PlanStore"] = None,
    read_etcd: Optional[Any] = None,
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

    audit = audit or _default_audit()
    auth = auth or GoogleAuth.from_env()
    tools_factory = tools_factory or _default_tools_factory(audit)
    agent = agent or build_default_agent()
    control = control if control is not None else _default_control_engine(audit)

    # Plan machinery (Phase 4). Reuses the engine's operator-namespace writer
    # and a read-only etcd facade. Built by default; injectable for tests.
    if read_etcd is None:
        from dsa_operator.etcd.read import connect_readonly
        read_etcd = connect_readonly(port=int(os.environ.get(
            "DSA_OPERATOR_ETCD_PORT", DEFAULT_LOCAL_ETCD_PORT)))
    if plan_store is None:
        plan_store = PlanStore(control._writer, read_etcd)  # type: ignore[attr-defined]

    # Autonomy supervisor (Phase 5). One app-level instance so its health /
    # alert state persists across ticks. Bound to the "supervisor" session:
    # in the web deployment that session never holds the executor lease, so
    # its monitoring loop runs but its mutating loops are gated out (a real
    # standing executor runs `python -m dsa_operator.monitor.supervisor`,
    # which acquires the lease as "supervisor"). All loops are OFF unless
    # enabled in config/policy.yaml.
    _SUP_SID = "supervisor"
    sup_tools = tools_factory("agent")
    sup_runner = PlanRunner(control, plan_store, read_etcd,
                            actor="agent", session_id=_SUP_SID)
    sup_cfg = AutonomyConfig.from_policy(control.policy)
    supervisor = AutonomySupervisor(
        control, sup_tools, audit, sup_cfg,
        plan_runner=sup_runner,
        injection=InjectionHealthCheck(control, sup_tools, audit,
                                       actor="agent", session_id=_SUP_SID,
                                       verify_after_s=sup_cfg.verify_after_s),
        actor="agent", session_id=_SUP_SID)

    # -- auth helpers ---------------------------------------------------------
    def current_user() -> Optional[str]:
        return session.get("user")

    def current_sid() -> str:
        sid = session.get("sid")
        if not sid:
            sid = new_session_id()
            session["sid"] = sid
        return sid

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
        session["sid"] = new_session_id()
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
        # Phase 6: hand the agent a control surface bound to THIS identity +
        # session. The engine still enforces lease/lockout/e-stop/gate, so a
        # non-executor's chat can propose but not execute.
        from dsa_operator.agent.control import AgentControl
        control_surface = AgentControl(control, plan_store, read_etcd,
                                       actor=user, session_id=current_sid())
        audit.record(AuditRecord(action="chat", kind="read", actor=user,
                                 params={"message": message}))
        try:
            resp = agent.chat(message, actor=user, tools=tools,
                              control=control_surface)
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

    # -- control plane (Phase 2, shadow-only) ---------------------------------
    @app.route("/api/policy")
    def api_policy():
        require_user()
        pol: Policy = control.policy
        actions = {
            name: {
                "gate": pol.gate_for(name),
                "target": spec.get("target"),
                "commissioning": spec.get("commissioning"),
                "reversible": bool(spec.get("reversible", False)),
                "two_person": pol.needs_two_person(name),
                "note": spec.get("note", ""),
            }
            for name, spec in pol.actions.items()
        }
        return jsonify({"ok": True, "data": {
            "version": pol.version, "mode": pol.mode,
            "paused": control.is_paused(), "actions": actions,
            "pointing": pol.pointing, "promoted": sorted(pol.promoted),
        }})

    @app.route("/api/authority")
    def api_authority():
        """What the dsa110-rt dashboard is asserting over the agent."""
        require_user()
        return jsonify({"ok": True, "data": control.authority().to_json()})

    @app.route("/api/observation")
    def api_observation():
        """Live recording status vs the dashboard's max_obs_seconds cap."""
        require_user()
        return jsonify({"ok": True, "data": control.observation_status().to_json()})

    @app.route("/api/lease")
    def api_lease():
        require_user()
        h = control.lease.holder()
        return jsonify({"ok": True, "data": {
            "holder": h.to_json() if h else None,
            "you_hold_it": bool(h and h.session_id == current_sid()),
        }})

    @app.route("/api/lease/acquire", methods=["POST"])
    def api_lease_acquire():
        user = require_user()
        auth = control.authority()
        if not auth.agents_enabled:
            return jsonify({"ok": False,
                            "error": "agent control is locked out from the dashboard"}), 403
        if auth.executor_email and user != auth.executor_email:
            return jsonify({"ok": False,
                            "error": f"the dashboard has pinned the executor to "
                                     f"{auth.executor_email}"}), 403
        ok = control.lease.acquire(user, current_sid())
        audit.record(AuditRecord(action="lease_acquire", kind="control",
                                 actor=user, ok=ok, mode="live"))
        h = control.lease.holder()
        return jsonify({"ok": ok, "data": {"holder": h.to_json() if h else None}})

    @app.route("/api/lease/release", methods=["POST"])
    def api_lease_release():
        user = require_user()
        ok = control.lease.release(current_sid())
        audit.record(AuditRecord(action="lease_release", kind="control",
                                 actor=user, ok=ok, mode="live"))
        return jsonify({"ok": ok})

    @app.route("/api/lease/takeover", methods=["POST"])
    def api_lease_takeover():
        user = require_user()
        prev = control.lease.holder()
        ok = control.lease.takeover(user, current_sid())
        audit.record(AuditRecord(
            action="lease_takeover", kind="control", actor=user, ok=ok,
            mode="live",
            note=f"seized from {prev.actor if prev else 'nobody'}"))
        return jsonify({"ok": ok})

    @app.route("/api/control", methods=["POST"])
    def api_control():
        user = require_user()
        body = request.get_json(silent=True) or {}
        action = (body.get("action") or "").strip()
        params = body.get("params") or {}
        if not action:
            return jsonify({"ok": False, "error": "missing action"}), 400
        if not isinstance(params, dict):
            return jsonify({"ok": False, "error": "params must be an object"}), 400
        decision = control.evaluate(action, params, actor=user,
                                    session_id=current_sid())
        return jsonify({"ok": True, "decision": decision.to_json()})

    @app.route("/api/approvals")
    def api_approvals():
        require_user()
        return jsonify({"ok": True, "data": control.approvals.pending()})

    @app.route("/api/approvals/request", methods=["POST"])
    def api_approval_request():
        user = require_user()
        body = request.get_json(silent=True) or {}
        action = (body.get("action") or "").strip()
        params = body.get("params") or {}
        if not control.policy.is_control_action(action):
            return jsonify({"ok": False, "error": "unknown action"}), 400
        ap = control.approvals.request(
            action, params, requested_by=user,
            n_required=control.policy.required_approvers(action),
            ttl_s=control.policy.approval_ttl_s,
            two_person=control.policy.needs_two_person(action),
        )
        audit.record(AuditRecord(action="approval_request", kind="approval",
                                 actor=user, params={"action": action},
                                 note=ap.id))
        return jsonify({"ok": True, "data": ap.to_json()})

    @app.route("/api/approvals/<approval_id>/grant", methods=["POST"])
    def api_approval_grant(approval_id):
        user = require_user()
        try:
            ap = control.approvals.grant(approval_id, user)
        except ApprovalError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        audit.record(AuditRecord(action="approval_grant", kind="approval",
                                 actor=user, params={"action": ap.action},
                                 note=approval_id))
        return jsonify({"ok": True, "data": ap.to_json()})

    @app.route("/api/pause", methods=["POST"])
    def api_pause():
        user = require_user()
        body = request.get_json(silent=True) or {}
        ok = control.pause(user, reason=str(body.get("reason", "")))
        return jsonify({"ok": ok, "paused": control.is_paused()})

    @app.route("/api/resume", methods=["POST"])
    def api_resume():
        user = require_user()
        if not control.lease.held_by(current_sid()):
            return jsonify({"ok": False,
                            "error": "only the executor may resume"}), 403
        ok = control.resume(user)
        return jsonify({"ok": ok, "paused": control.is_paused()})

    # -- observing plan (Phase 4) ---------------------------------------------
    @app.route("/api/observability")
    def api_observability():
        require_user()
        try:
            dec = float(request.args["dec"])
        except (KeyError, ValueError):
            return jsonify({"ok": False, "error": "dec (deg) is required"}), 400
        ra = request.args.get("ra", type=float)
        pt = control.policy.pointing
        obs = astro.observability(
            dec, ra_deg=ra, now_unix=time.time(),
            el_min=float(pt.get("el_min_deg", 30.0)),
            el_max=float(pt.get("el_max_deg", 125.0)),
            lat_deg=float(pt.get("lat_ovro_deg", astro.OVRO_LAT_DEG)))
        return jsonify({"ok": True, "data": obs.to_json()})

    @app.route("/api/plan")
    def api_plan_get():
        require_user()
        plan = plan_store.get()
        if plan is None:
            return jsonify({"ok": True, "data": {"plan": None}})
        now = time.time()
        active = plan.active_at(now)
        nxt = plan.next_segment(now)
        return jsonify({"ok": True, "data": {
            "plan": plan.to_json(),
            "active_now": active.to_json() if active else None,
            "dec_now": plan.dec_at(now),
            "next_segment": nxt.to_json() if nxt else None,
            "lst_now_deg": round(astro.lst_deg(now), 4),
        }})

    def _require_executor():
        if not control.lease.held_by(current_sid()):
            abort(403)

    @app.route("/api/plan", methods=["POST"])
    def api_plan_set():
        user = require_user()
        _require_executor()
        body = request.get_json(silent=True) or {}
        pt = control.policy.pointing
        kw = dict(el_min=float(pt.get("el_min_deg", 30.0)),
                  el_max=float(pt.get("el_max_deg", 125.0)),
                  lat_deg=float(pt.get("lat_ovro_deg", astro.OVRO_LAT_DEG)))
        try:
            if body.get("sources"):
                plan = ObservingPlan.from_sources(
                    body["sources"], after_unix=time.time(), created_by=user,
                    default_window_min=float(body.get("window_min", 30.0)),
                    note=str(body.get("note", "")))
            else:
                plan = ObservingPlan.from_segments(
                    body.get("segments", []), created_by=user,
                    note=str(body.get("note", "")))
            plan.validate(**kw)
        except (PlanError, KeyError, ValueError, TypeError) as exc:
            return jsonify({"ok": False, "error": f"invalid plan: {exc}"}), 400
        plan_store.set(plan)
        audit.record(AuditRecord(action="set_observing_plan", kind="control",
                                 actor=user, mode="live",
                                 params={"n_segments": len(plan.segments)}))
        return jsonify({"ok": True, "data": {"n_segments": len(plan.segments),
                                             "plan": plan.to_json()}})

    @app.route("/api/plan/clear", methods=["POST"])
    def api_plan_clear():
        user = require_user()
        _require_executor()
        plan_store.clear()
        audit.record(AuditRecord(action="clear_observing_plan", kind="control",
                                 actor=user, mode="live"))
        return jsonify({"ok": True})

    @app.route("/api/plan/tick", methods=["POST"])
    def api_plan_tick():
        user = require_user()
        _require_executor()
        runner = PlanRunner(control, plan_store, read_etcd,
                            actor=user, session_id=current_sid())
        result = runner.apply()
        return jsonify({"ok": True, "data": result.to_json()})

    @app.route("/api/plan/preview", methods=["POST", "GET"])
    def api_plan_preview():
        user = require_user()
        runner = PlanRunner(control, plan_store, read_etcd,
                            actor=user, session_id=current_sid())
        return jsonify({"ok": True, "data": runner.decide().to_json()})

    # -- autonomy supervisor (Phase 5) ----------------------------------------
    @app.route("/api/autonomy")
    def api_autonomy():
        require_user()
        return jsonify({"ok": True, "data": supervisor.status()})

    @app.route("/api/autonomy/tick", methods=["POST"])
    def api_autonomy_tick():
        """Force one supervisor tick. Monitoring always runs; the mutating
        loops only act if the supervisor session holds the lease (so from
        the web this is effectively a monitor-only refresh)."""
        require_user()
        tick = supervisor.tick()
        return jsonify({"ok": True, "data": tick.to_json()})

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "phase": 5, "authed": bool(current_user())})

    return app


def main() -> int:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dsa_operator.audit.egress import maybe_install_from_env
    from dsa_operator.env import load_secrets
    load_secrets()
    maybe_install_from_env()
    app = create_app()
    host = os.environ.get("DSA_OPERATOR_BIND", "127.0.0.1")
    port = int(os.environ.get("DSA_OPERATOR_PORT", "8787"))
    app.run(host=host, port=port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
