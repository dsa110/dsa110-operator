# dsa110-operator

An agent-driven control and monitoring console for the DSA-110 real-time
system. It is meant to run on **any operator laptop** and to reach the
observatory **only over SSH to `h23`** — never any other host, and
(deliberately) never the `lxd110h20` web UI. Through that one SSH hop it
forwards to etcd and the `h23` `dsa_monitor` dashboard, and reads data
products. It can move the array, reconfigure / start / stop observing,
monitor the system, and assess performance.

The brain is **Claude** (via the Claude Agent SDK, on your own Anthropic
account). Many people can connect to **monitor and ask questions**; only
**one** session at a time may **execute control actions**, arbitrated by
an etcd lease. Risky/irreversible actions are gated by a human-readable,
machine-enforced **policy** (`config/policy.yaml`), and **everything** is
logged.

> **Status: Phase 6.** Everything in Phase 5 plus a **controlling Claude
> brain**: the web chat now hands the agent an `AgentControl` surface so it
> can *propose and run* control actions (`propose_action`), *request* (never
> grant) approvals, and drive the observing plan conversationally — every
> call funnelled through the same `ControlEngine` gauntlet (lease, dashboard
> lockout, e-stop, gate, approval, shadow/live), so handing the model these
> tools changes *who can ask*, never *what is allowed*.
>
> Phase 5 added the **autonomy supervisor**: a deterministic, non-LLM loop
> (`src/dsa_operator/monitor/`)
> that continuously assesses **health** (fleet, static-sky, SEFD,
> observation-time cap) and alerts on edge-triggered changes; optionally
> **auto-recovers** known failures, runs periodic **injection
> health-checks** (end-to-end pulse tests), and ticks the **observing-plan
> runner** on a cadence. Every loop is **off by default**; monitoring is
> read-only, and the three mutating loops act only when their flag is set
> **and** this session holds the lease **and** the dashboard hasn't locked
> agents out **and** the e-stop is clear — and even then each action runs
> the full gate engine (so a "recovery" during commissioning surfaces as
> *needs approval*). A standing executor runs
> `python -m dsa_operator.monitor.supervisor`. See `docs/OPERATOR_AGENT.md`.

## Human authority & the API key

* **Dashboard lockout / executor pin / time cap.** Humans control the agent
  from the dsa110-rt dashboard via a single etcd key `/cmd/operator/control`
  (`agents_enabled`, `executor_email`, `max_obs_seconds`). It lives outside
  every prefix the operator can write, so the agent can never re-enable
  itself, re-point the executor, or extend its own time limit.
* **One key, many users.** The single server process holds one Anthropic
  key; monitoring users sign in over Google SSO and never receive a key.
  Provision it via `ANTHROPIC_API_KEY` or a git-ignored secrets file (see
  `.env.example`); nothing secret is logged or committed.

## What this is NOT allowed to touch

* No host other than `h23` (single SSH hop; etcd + dashboard are reached
  *through* `h23`).
* No interaction with the `lxd110h20` web UI.
* No outbound network except the explicit allowlist in
  `config/egress_allowlist.yaml` (Anthropic API, Google OAuth, Slack
  webhook, and SSH to `h23`).

## Layout

| Path | Purpose |
| --- | --- |
| `src/dsa_operator/transport/` | SSH tunnel manager (`-L` forwards through `h23`). |
| `src/dsa_operator/etcd/` | Read-only etcd client over the forwarded port. |
| `src/dsa_operator/tools/` | Typed tool surface the agent is allowed to call. |
| `src/dsa_operator/audit/` | Append-only local log + etcd audit + Slack notify. |
| `src/dsa_operator/agent/` | The Claude brain + deterministic stub fallback (read-only tools). |
| `src/dsa_operator/control/` | Single-executor lease, gate engine, approvals, typed verbs, and the live executor (dashboard delegation + `/cmd/ant` pointing). |
| `src/dsa_operator/observing/` | Sidereal/transit math, the observing-plan model + etcd persistence, and the plan runner (drives pointing via the engine). |
| `src/dsa_operator/monitor/` | Autonomy supervisor: non-LLM health/recovery/injection loops + plan ticking, all gated through the engine. |
| `src/dsa_operator/web/` | Flask console + Google SSO + assistant chat (read-only). |
| `config/policy.yaml` | Capability document as code (the approval gates). |
| `config/egress_allowlist.yaml` | The only outbound endpoints permitted. |
| `docs/OPERATOR_AGENT.md` | Operator runbook (generated contract + playbooks). |

## Quick start

```bash
pip install -e '.[etcd,web,agent,dev]'
# open the tunnel to h23 (forwards etcd + dashboard locally)
python -m dsa_operator.transport.ssh_tunnel --ssh-host h23
# smoke the read-only tools (CLI)
python -m dsa_operator.tools.readonly --demo
pytest -q
```

### Web console (Phase 1)

```bash
export GOOGLE_CLIENT_ID=...  GOOGLE_CLIENT_SECRET=...
export DSA_OPERATOR_ALLOWED_DOMAINS=dsa110.org     # or _ALLOWED_EMAILS=a@x,b@y
export DSA_OPERATOR_REDIRECT_URI=http://localhost:8787/auth/callback
export ANTHROPIC_API_KEY=sk-ant-...                # optional; stub agent if unset
python -m dsa_operator.web.app                     # 127.0.0.1:8787
```

Any allow-listed Google account gets read-only views + the assistant chat;
unlisted accounts are denied and audited.

### Autonomy supervisor (Phase 5)

```bash
# enable loops in config/policy.yaml (autonomy:), then run the standing
# executor — it acquires the lease as session "supervisor":
python -m dsa_operator.monitor.supervisor
```

From the web console, `GET /api/autonomy` shows supervisor status and
`POST /api/autonomy/tick` forces a monitor refresh (mutating loops stay
gated unless the *supervisor* session holds the lease).

## Deployment

Three user systemd units (tunnel, web console, standing supervisor) live in
`deploy/` with an install runbook in `deploy/README.md`. Secrets come from a
git-ignored `EnvironmentFile`; set `DSA_OPERATOR_ENFORCE_EGRESS=1` to arm the
in-process egress tripwire and `DSA_OPERATOR_SLACK_WEBHOOK_URL` for notifications
(`python -m dsa_operator.audit.slack --test "hi"` to verify). The console is
tabbed: **Monitor** (live views + chat), **Control** (lease / authority /
e-stop / propose-action / approvals), **Plan**, and **Autonomy**.

Promote actions from `commissioning` to live per-action via
`config/local.yaml` (see `config/local.yaml.example` for the staged ladder).

## Design

See the design discussion captured in the team chat and
`docs/OPERATOR_AGENT.md`. Security posture in brief: typed/allow-listed
tools only (no raw shell, no raw etcd handed to the model), read-only by
default, single-executor etcd lease, Google SSO identities in every log
line, no raw telemetry sent to the model, and a global pause/e-stop.
