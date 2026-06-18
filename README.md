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

> **Status: Phase 1.** Read-only foundation + an authenticated,
> multi-user **web console** (Flask + Google SSO) with an assistant chat
> over the read-only tools. There is still **no control surface**;
> mutating tools arrive (in shadow mode first) in later phases. See
> `docs/OPERATOR_AGENT.md` for the capability roadmap.

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
| `src/dsa_operator/monitor/` | (later) local, non-LLM health/injection/recovery loops. |
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
unlisted accounts are denied and audited. The console has **no mutating
routes** other than chat/login/logout.

## Design

See the design discussion captured in the team chat and
`docs/OPERATOR_AGENT.md`. Security posture in brief: typed/allow-listed
tools only (no raw shell, no raw etcd handed to the model), read-only by
default, single-executor etcd lease, Google SSO identities in every log
line, no raw telemetry sent to the model, and a global pause/e-stop.
