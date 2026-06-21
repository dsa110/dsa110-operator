"""Operator web console — Flask, local-only (no SSO).

The console runs on the operator's own laptop, bound to loopback and reached
through the SSH tunnel to h23, so there is nothing to authenticate against: the
identity is just the local operator's name (see
:mod:`dsa_operator.web.identity`), stamped into every audit row. Monitoring +
chat are open; control actions are gated by the single-executor lease.

The app factory takes injectable ``operator`` / ``tools_factory`` / ``agent`` /
``audit`` so tests run with fakes (no network, no live etcd).
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from typing import Any, Callable, Optional

from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    session,
)

from dsa_operator import DEFAULT_LOCAL_DASHBOARD_PORT, DEFAULT_LOCAL_ETCD_PORT
from dsa_operator.agent import build_default_agent
from dsa_operator.agent.base import Agent
from dsa_operator.audit.log import AuditLog, AuditRecord
from dsa_operator.control.approvals import ApprovalError, ApprovalStore
from dsa_operator.control.engine import ControlEngine
from dsa_operator.control.lease import (
    DEFAULT_TTL_S,
    ExecutorLease,
    new_session_id,
)
from dsa_operator.monitor.injection import InjectionHealthCheck
from dsa_operator.monitor.supervisor import AutonomyConfig, AutonomySupervisor
from dsa_operator.observing import astro
from dsa_operator.observing.plan import ObservingPlan, PlanError, PlanStore
from dsa_operator.observing.runner import PlanRunner
from dsa_operator.policy import Policy, load_policy
from dsa_operator.tools.readonly import ReadOnlyTools, ToolError
from dsa_operator.web.identity import resolve_operator

LOG = logging.getLogger("dsa_operator.web")

ToolsFactory = Callable[[str], ReadOnlyTools]


def _etcd_host() -> str:
    """Where etcd lives. Defaults to loopback (the SSH-tunnel-forwarded port);
    set ``DSA_OPERATOR_ETCD_HOST`` to reach it directly — e.g. running ON h23,
    set it to ``etcdv3service.pro.pvt`` with ``DSA_OPERATOR_ETCD_PORT=2379``."""
    return os.environ.get("DSA_OPERATOR_ETCD_HOST", "127.0.0.1")


def _etcd_port() -> int:
    return int(os.environ.get("DSA_OPERATOR_ETCD_PORT", DEFAULT_LOCAL_ETCD_PORT))


def _dash_port() -> int:
    return int(os.environ.get("DSA_OPERATOR_DASHBOARD_PORT",
                              DEFAULT_LOCAL_DASHBOARD_PORT))


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

    etcd_host = _etcd_host()
    etcd_port = _etcd_port()
    dash_port = _dash_port()
    writer = connect_writer(host=etcd_host, port=etcd_port)
    # Mirror audit rows into etcd's /operator/audit trail too.
    audit._etcd_sink = audit._etcd_sink or EtcdAuditSink(writer)  # type: ignore[attr-defined]

    read = connect_readonly(host=etcd_host, port=etcd_port)
    executor = LiveExecutor(
        dashboard=DashboardControlClient(port=dash_port),
        control_etcd=ControlEtcdWriter(etcd_host, etcd_port),
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

    etcd = connect_readonly(host=_etcd_host(), port=_etcd_port())
    dash = DashboardClient(port=_dash_port())

    def factory(actor: str) -> ReadOnlyTools:
        return ReadOnlyTools(etcd, dash, audit, actor=actor)

    return factory


def create_app(
    *,
    operator: Optional[str] = None,
    tools_factory: Optional[ToolsFactory] = None,
    agent: Optional[Agent] = None,
    audit: Optional[AuditLog] = None,
    control: Optional[ControlEngine] = None,
    plan_store: Optional["PlanStore"] = None,
    read_etcd: Optional[Any] = None,
    secret_key: Optional[str] = None,
    lease_keepalive: bool = False,
    observing_autopilot: bool = False,
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
    # Local-only identity: the console runs on the operator's own laptop bound
    # to loopback, so there is no SSO. The name is just an audit/lease label.
    operator_name = resolve_operator(operator)
    tools_factory = tools_factory or _default_tools_factory(audit)
    agent = agent or build_default_agent()
    control = control if control is not None else _default_control_engine(audit)

    # Plan machinery (Phase 4). Reuses the engine's operator-namespace writer
    # and a read-only etcd facade. Built by default; injectable for tests.
    if read_etcd is None:
        from dsa_operator.etcd.read import connect_readonly
        read_etcd = connect_readonly(host=_etcd_host(), port=_etcd_port())
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
    _sup_slack = None
    if os.environ.get("DSA_OPERATOR_SLACK_WEBHOOK_URL") \
            or os.environ.get("DSA_OPERATOR_SLACK_WEBHOOK"):
        from dsa_operator.audit.slack import SlackNotifier
        _sup_slack = SlackNotifier()
    supervisor = AutonomySupervisor(
        control, sup_tools, audit, sup_cfg,
        plan_runner=sup_runner, slack=_sup_slack,
        injection=InjectionHealthCheck(control, sup_tools, audit,
                                       actor="agent", session_id=_SUP_SID,
                                       verify_after_s=sup_cfg.verify_after_s),
        actor="agent", session_id=_SUP_SID)

    # -- identity helpers -----------------------------------------------------
    # No login: every request is the local operator. Each browser session still
    # gets its own session id so two tabs/browsers on one laptop arbitrate the
    # executor lease independently.
    def current_user() -> str:
        return operator_name

    def current_sid() -> str:
        sid = session.get("sid")
        if not sid:
            sid = new_session_id()
            session["sid"] = sid
        return sid

    def require_user() -> str:
        return operator_name

    def _tools_for_request() -> ReadOnlyTools:
        return tools_factory(require_user())

    # -- pages ----------------------------------------------------------------
    @app.route("/")
    def index():
        return render_template("console.html", user=operator_name,
                               agent_model=getattr(agent, "model", "?"))

    @app.route("/api/whoami")
    def whoami():
        return jsonify({"user": require_user()})

    @app.route("/api/status")
    def api_status():
        """Compact roll-up for the top status bar: policy mode, e-stop,
        dashboard system_state, antenna motion, pointing, the executor lease,
        and the observing-plan stage — one poll instead of five."""
        require_user()
        out: dict[str, Any] = {"mode": getattr(control.policy, "mode", "?")}
        try:
            out["paused"] = control.is_paused()
        except Exception:                                  # noqa: BLE001
            out["paused"] = None
        tools = _tools_for_request()
        try:
            fleet = tools.get_fleet_status()
            out["system_state"] = (fleet or {}).get("system_state")
            out["corr"] = (fleet or {}).get("corr")
            out["search"] = (fleet or {}).get("search")
        except Exception as exc:                           # noqa: BLE001
            out["system_state"] = {"error": str(exc)}
        try:
            pt = tools.get_array_pointing() or {}
            out["pointing"] = {
                "target_dec_deg": pt.get("target_dec_deg"),
                "mean_commanded_el_deg": pt.get("mean_commanded_el_deg"),
                "n_not_settled": pt.get("n_not_settled"),
                "n_antennas_reporting": pt.get("n_antennas_reporting"),
            }
        except Exception as exc:                           # noqa: BLE001
            out["pointing"] = {"error": str(exc)}
        try:
            h = control.lease.holder()
            out["lease"] = {
                "holder": h.to_json() if h else None,
                "you_hold_it": bool(h and h.session_id == current_sid()),
            }
        except Exception:                                  # noqa: BLE001
            out["lease"] = None
        try:
            plan = plan_store.get()
            if plan is not None:
                now = time.time()
                active = plan.active_at(now)
                out["plan"] = {
                    "armed": plan.armed, "armed_by": plan.armed_by,
                    "n_segments": len(plan.segments),
                    "active_dec": active.dec_deg if active else None,
                    "active_label": active.label if active else None,
                }
            else:
                out["plan"] = None
        except Exception:                                  # noqa: BLE001
            out["plan"] = None
        return jsonify({"ok": True, "data": out})

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
                                       actor=user, session_id=current_sid(),
                                       tools=tools)
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
            "ttl_s": getattr(control.lease, "_ttl", DEFAULT_TTL_S),
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

    @app.route("/api/plan/step", methods=["POST"])
    def api_plan_step():
        """Advance the ARMED plan's full bring-up by one step (point -> fstable
        -> modes -> start/restart -> warm -> arm) and report the current stage
        / any blocker. The console autopilot normally does this on a cadence;
        this lets a human nudge or inspect it. Executor only."""
        user = require_user()
        _require_executor()
        from dsa_operator.observing.session import (
            ObservingSequencer, ToolsSiteState)
        seq = ObservingSequencer(control, plan_store,
                                 ToolsSiteState(tools_factory(user)),
                                 actor=user, session_id=current_sid())
        return jsonify({"ok": True, "data": seq.apply().to_json()})

    @app.route("/api/plan/preview", methods=["POST", "GET"])
    def api_plan_preview():
        user = require_user()
        runner = PlanRunner(control, plan_store, read_etcd,
                            actor=user, session_id=current_sid())
        return jsonify({"ok": True, "data": runner.decide().to_json()})

    @app.route("/api/plan/sequence", methods=["GET", "POST"])
    def api_plan_sequence():
        """Per-segment bring-up preview (point/fstable/modes/start/warm/arm)
        for the staged plan — what to confirm before arming."""
        user = require_user()
        from dsa_operator.observing.session import (
            ObservingSequencer, ToolsSiteState)
        seq = ObservingSequencer(control, plan_store,
                                 ToolsSiteState(tools_factory(user)),
                                 actor=user, session_id=current_sid())
        return jsonify({"ok": True, "data": seq.describe_plan()})

    @app.route("/api/plan/arm", methods=["POST"])
    def api_plan_arm():
        user = require_user()
        _require_executor()
        plan = plan_store.arm(by=user, now=time.time())
        if plan is None:
            return jsonify({"ok": False, "error": "no staged plan to arm"}), 400
        audit.record(AuditRecord(action="arm_observing_plan", kind="control",
                                 actor=user, mode="live",
                                 params={"n_segments": len(plan.segments)}))
        return jsonify({"ok": True, "data": {"armed": True,
                                             "plan": plan.to_json()}})

    @app.route("/api/plan/disarm", methods=["POST"])
    def api_plan_disarm():
        user = require_user()
        _require_executor()
        plan = plan_store.disarm()
        audit.record(AuditRecord(action="disarm_observing_plan", kind="control",
                                 actor=user, mode="live"))
        return jsonify({"ok": True, "data": {
            "armed": False, "plan": plan.to_json() if plan else None}})

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

    # -- lease keepalive ------------------------------------------------------
    # While this process holds the executor lease, refresh it on a cadence well
    # under the TTL so it doesn't lapse during normal use. If the laptop sleeps
    # the thread freezes, the lease expires on h23 (control auto-frees — good),
    # and on wake keepalive() reports "lost" so the UI prompts a re-acquire.
    def _lease_keepalive_loop(stop: threading.Event) -> None:
        ttl = getattr(control.lease, "_ttl", DEFAULT_TTL_S)
        period = max(2.0, float(ttl) / 3.0)
        while not stop.wait(period):
            try:
                state = control.lease.keepalive()
                if state == "lost":
                    LOG.warning("executor lease lapsed (laptop asleep / taken "
                                "over) — control released")
            except Exception:                              # noqa: BLE001
                LOG.exception("lease keepalive failed (continuing)")

    if lease_keepalive:
        _ka_stop = threading.Event()
        app._lease_keepalive_stop = _ka_stop               # type: ignore[attr-defined]
        threading.Thread(target=_lease_keepalive_loop, args=(_ka_stop,),
                         daemon=True, name="lease-keepalive").start()

    # -- observing autopilot --------------------------------------------------
    # Arming a plan only flips a flag; SOMETHING has to tick the bring-up
    # sequencer (point -> fstable -> modes -> start/restart -> warm -> arm) for
    # the array to actually come up. A standing executor on h23 does this when
    # it holds the lease, but in the laptop/web deployment the operator holds
    # the lease (they had to, to arm), so nothing was driving the sequence —
    # an armed plan just sat there. This loop closes that gap: when THIS
    # console process holds the executor lease and a plan is armed, it advances
    # the sequencer on the plan cadence, acting as the lease holder. It stays
    # idle when an external process (e.g. h23) holds the lease, so the two
    # never both drive — only the single lease holder does. Every step still
    # goes through the full ControlEngine gauntlet (e-stop, lockout, gate,
    # shadow/live), so this widens *nothing* the operator couldn't already do.
    def _observing_autopilot_loop(stop: threading.Event) -> None:
        from dsa_operator.observing.session import (
            ObservingSequencer, ToolsSiteState)
        cfg = AutonomyConfig.from_policy(control.policy)
        period = max(5.0, float(cfg.plan_s))
        st: dict[str, Any] = {"sid": None, "seq": None}
        while not stop.wait(period):
            try:
                if not control.lease.mine():
                    st["sid"] = None
                    st["seq"] = None
                    continue
                plan = plan_store.get()
                if plan is None or not plan.armed:
                    st["sid"] = None
                    st["seq"] = None
                    continue
                holder = control.lease.holder()
                if holder is None:
                    continue
                if st["seq"] is None or st["sid"] != holder.session_id:
                    st["sid"] = holder.session_id
                    st["seq"] = ObservingSequencer(
                        control, plan_store,
                        ToolsSiteState(tools_factory(holder.actor)),
                        actor=holder.actor, session_id=holder.session_id)
                res = st["seq"].apply()
                step = (res.to_json() or {}).get("step") or {}
                if step.get("action") or step.get("blocked"):
                    LOG.info("observing autopilot: stage=%s — %s",
                             res.stage, step.get("detail", res.reason))
            except Exception:                              # noqa: BLE001
                LOG.exception("observing autopilot tick failed (continuing)")

    if observing_autopilot:
        _ap_stop = threading.Event()
        app._observing_autopilot_stop = _ap_stop           # type: ignore[attr-defined]
        threading.Thread(target=_observing_autopilot_loop, args=(_ap_stop,),
                         daemon=True, name="observing-autopilot").start()

    return app


def main() -> int:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dsa_operator.audit.egress import maybe_install_from_env
    from dsa_operator.env import load_secrets
    load_secrets()
    maybe_install_from_env()
    app = create_app(lease_keepalive=True, observing_autopilot=True)
    host = os.environ.get("DSA_OPERATOR_BIND", "127.0.0.1")
    port = int(os.environ.get("DSA_OPERATOR_PORT", "8787"))
    app.run(host=host, port=port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
