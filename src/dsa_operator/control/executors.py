"""Live executors — the ONLY code that can mutate observatory state.

A :class:`Plan` (from :mod:`dsa_operator.control.verbs`) is a list of
:class:`Step`s. The :class:`LiveExecutor` is the single place those steps
become real actions, and it has exactly two narrow capabilities:

* **Dashboard delegation.** Almost every control verb already has an
  audited, confirm-gated route on the ``dsa_monitor`` dashboard (start/stop
  fleet, utc_start/stop, dumps, dump_now, fstable build/deploy, injections,
  spectral line, bounce, fleet code update). The executor POSTs the
  verb's form to that route — reusing the observatory's own orchestration
  (ARM_SEQ computation, ssh cleanup, UDP triggers, rsync, K-calibration)
  rather than reimplementing it.

* **One direct etcd key class.** Antenna pointing has *no* dashboard
  route, so the executor writes ``/cmd/ant/{n}`` directly — and
  :class:`ControlEtcdWriter` allow-lists **only** the ``/cmd/ant/``
  prefix. That single key class is the entire direct control-key write
  surface of this whole project.

The executor never runs a raw shell, never writes an arbitrary etcd key,
and is only ever invoked by :class:`~dsa_operator.control.engine.ControlEngine`
*after* the full lease/e-stop/gate/approval gauntlet AND only for an action
that has been explicitly promoted to live. With no executor injected (the
default), live execution is impossible.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional, Protocol
from urllib.parse import urlparse

from dsa_operator import DEFAULT_LOCAL_DASHBOARD_PORT, DEFAULT_LOCAL_ETCD_PORT
from dsa_operator.control.verbs import Plan, Step
from dsa_operator.etcd.read import ReadOnlyEtcd

LOG = logging.getLogger("dsa_operator.control.executors")

# The complete set of control keys the operator may write DIRECTLY. Anything
# else must go through an audited dashboard route.
ALLOWED_CONTROL_PREFIXES = ("/cmd/ant/",)

_LOOPBACK = {"127.0.0.1", "localhost"}


class ExecutorError(RuntimeError):
    pass


def _check_dashboard_result(target: str, result: Any) -> None:
    """Raise unless the dashboard actually *accepted* the command.

    The dsa_monitor ``/control/`` routes answer a form POST with an HTTP
    status **and** a JSON body carrying their own ``ok`` flag. Three distinct
    failures all mean *nothing happened on the array*:

    * a wrong/absent route -> HTTP 404 (only start/stop/utc_start/utc_stop and
      a handful of others are actually exposed),
    * a server-side error -> HTTP 5xx,
    * an application-level refusal -> HTTP 200 but ``{"ok": false, "error": …}``
      (e.g. ``utc_start`` with no captures answering, or ``stop`` without the
      ``confirm`` token).

    Previously the executor ignored all of these and reported success, so the
    bring-up sequencer advanced as though the fleet had come up while the POST
    had in fact 404'd or been refused — an armed plan "completed" with zero
    real effect. Fail loudly instead: the engine then audits a failure and the
    sequencer blocks with the real reason.
    """
    if not isinstance(result, dict):
        return
    code = result.get("status_code")
    if isinstance(code, int) and not (200 <= code < 300):
        body = result.get("json")
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("error") or body.get("message") or "")
        if not detail:
            detail = str(result.get("text") or "")[:300]
        hint = ("  (route not exposed on the dsa_monitor dashboard?)"
                if code == 404 else "")
        raise ExecutorError(
            f"dashboard POST {target} returned HTTP {code}"
            + (f": {detail}" if detail else "") + hint)
    body = result.get("json")
    if isinstance(body, dict) and body.get("ok") is False:
        raise ExecutorError(
            f"dashboard POST {target} refused: "
            + str(body.get("error") or body.get("message") or body))


# --- direct etcd control writer (antenna pointing only) --------------------

def _check_control_key(key: str) -> None:
    if not any(key.startswith(p) for p in ALLOWED_CONTROL_PREFIXES):
        raise ExecutorError(
            f"refusing direct etcd write to {key!r}: the operator may write "
            f"only {ALLOWED_CONTROL_PREFIXES} directly (everything else goes "
            f"through an audited dashboard route)")


class ControlEtcdWriter:
    """Writes ``/cmd/ant/{n}`` with ``DsaStore``-compatible JSON encoding."""

    def __init__(self, host: str, port: int) -> None:
        os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
        import etcd3  # lazy

        self._c = etcd3.client(host=host, port=port)

    def put_dict(self, key: str, value: dict[str, Any]) -> None:
        _check_control_key(key)
        self._c.put(key, json.dumps(value))


class FakeControlEtcd:
    """Records control puts for tests; enforces the same allowlist."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, dict[str, Any]]] = []

    def put_dict(self, key: str, value: dict[str, Any]) -> None:
        _check_control_key(key)
        self.puts.append((key, dict(value)))


# --- dashboard control client (form POST) ----------------------------------

class FormPoster(Protocol):
    def __call__(self, url: str, form: dict[str, Any], timeout: float
                 ) -> dict[str, Any]: ...


def _requests_post_form(url: str, form: dict[str, Any],
                        timeout: float) -> dict[str, Any]:
    import requests  # lazy

    resp = requests.post(url, data=form, timeout=timeout)
    out: dict[str, Any] = {"status_code": resp.status_code,
                           "ok": resp.ok}
    try:
        out["json"] = resp.json()
    except ValueError:
        out["text"] = resp.text[:500]
    return out


class DashboardControlClient:
    """Loopback-pinned POST client for the dsa_monitor ``/control/`` routes."""

    def __init__(self, port: int = DEFAULT_LOCAL_DASHBOARD_PORT, *,
                 host: str = "127.0.0.1", timeout_s: float = 30.0,
                 poster: Optional[FormPoster] = None) -> None:
        if host not in _LOOPBACK:
            raise ValueError(f"dashboard host {host!r} is not loopback")
        self.base = f"http://{host}:{int(port)}"
        self.timeout_s = timeout_s
        self._post = poster or _requests_post_form

    def post(self, path: str, form: dict[str, Any]) -> dict[str, Any]:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base + path
        if urlparse(url).hostname not in _LOOPBACK:
            raise ExecutorError(f"refusing non-loopback dashboard URL: {url}")
        return self._post(url, form, self.timeout_s)


class FakeDashboardControl:
    """Records form POSTs for tests.

    ``responder`` (optional) lets a test simulate the real dashboard's reply
    for a given path — e.g. a 404 for an unexposed route or an
    ``{"ok": false}`` refusal — so the executor's result-checking can be
    exercised. It receives ``(path, form)`` and returns the result dict; if it
    returns ``None`` the default 200/ok reply is used.
    """

    def __init__(self, responder: Optional[Any] = None) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._responder = responder

    def post(self, path: str, form: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, dict(form)))
        if self._responder is not None:
            resp = self._responder(path, dict(form))
            if resp is not None:
                return resp
        return {"status_code": 200, "ok": True}


# --- the executor ----------------------------------------------------------

class LiveExecutor:
    def __init__(
        self, *,
        dashboard: Any,                 # DashboardControlClient | fake
        control_etcd: Any,              # ControlEtcdWriter | fake
        read_etcd: Optional[ReadOnlyEtcd] = None,
    ) -> None:
        self._dash = dashboard
        self._control = control_etcd
        self._read = read_etcd

    def execute(self, plan: Plan, *, actor: str) -> dict[str, Any]:
        results = [self._do(step, actor) for step in plan.steps]
        return {"action": plan.action, "n_steps": len(plan.steps),
                "results": results}

    # -- per-step dispatch ----------------------------------------------------
    def _do(self, step: Step, actor: str) -> dict[str, Any]:
        if step.kind == "dashboard_post":
            form = dict(step.payload)
            form.setdefault("user", actor)
            result = self._dash.post(step.target, form)
            _check_dashboard_result(step.target, result)
            return {"kind": "dashboard_post", "target": step.target,
                    "result": result}
        if step.kind == "etcd_put":
            return self._etcd_put(step, actor)
        raise ExecutorError(
            f"live executor refuses step kind {step.kind!r} for {step.target!r}")

    def _etcd_put(self, step: Step, actor: str) -> dict[str, Any]:
        if step.target == "/cmd/ant/<all>":
            ants = self._antenna_list(step.payload.get("refants"))
            payload = {k: step.payload[k] for k in ("cmd", "val")
                       if k in step.payload}
            for ant in ants:
                self._control.put_dict(f"/cmd/ant/{ant}", payload)
            return {"kind": "etcd_put", "target": "/cmd/ant/*",
                    "n_antennas": len(ants), "payload": payload}
        # single concrete control key (still allowlist-checked by the writer)
        self._control.put_dict(step.target, dict(step.payload))
        return {"kind": "etcd_put", "target": step.target}

    def _antenna_list(self, refants: Any) -> list[int]:
        """Antenna numbers to command, from ``/cnf/corr.antenna_order``."""
        if self._read is None:
            raise ExecutorError("no read-etcd available to resolve antenna list")
        corr = self._read.get_dict("/cnf/corr")
        order = (corr or {}).get("antenna_order") if isinstance(corr, dict) else None
        if not isinstance(order, dict) or not order:
            raise ExecutorError("could not read /cnf/corr.antenna_order")
        ants = [int(v) for v in order.values()]
        skip = set()
        if isinstance(refants, (list, tuple)):
            skip = {int(x) for x in refants}
        return [a for a in ants if a not in skip]


__all__ = [
    "LiveExecutor",
    "ControlEtcdWriter",
    "FakeControlEtcd",
    "DashboardControlClient",
    "FakeDashboardControl",
    "ExecutorError",
    "ALLOWED_CONTROL_PREFIXES",
]
