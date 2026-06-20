# dsa110-operator

An agent-driven control and monitoring console for the DSA-110 real-time
system, powered by **Claude**. It runs on **your own machine** and reaches
the observatory **only over SSH to `h23`** — never any other host, and never
the `lxd110h20` web UI. It can monitor the system, move the array, and
start/stop/reconfigure observing, with every action gated by a
machine-enforced policy and fully logged.

## Documentation

| Doc | What's in it |
| --- | --- |
| **[INSTALL](docs/INSTALL.md)** | Prerequisites, install, SSH tunnel, secrets, identity, multi-user, surviving sleep, running as a service. |
| **[USAGE](docs/USAGE.md)** | The console tabs, taking control, running actions, approvals, observing plans, autonomy, kill switches, troubleshooting. |
| **[CAPABILITIES](docs/CAPABILITIES.md)** | Exactly what the agent can and cannot do (the human-readable face of `config/policy.yaml`). |
| [`deploy/README.md`](deploy/README.md) | systemd units + ops runbook. |

## How it works

```
your machine ──ssh──> h23 ──┬── etcd (etcdv3service.pro.pvt:2379)   [forwarded to localhost:12379]
                            ├── dsa_monitor dashboard (:5778)        [forwarded to localhost:15778]
                            └── data products on /dataz             [over the same ssh]
```

- **Many watchers, one controller.** Each person runs their own console on
  their own laptop (no login) to monitor and ask the assistant questions. Only
  **one** session at a time may execute control actions, arbitrated by a lease
  in `h23`'s etcd — so even with several consoles running on different machines,
  control is always
  singular. No shared server is required.
- **One Claude account.** A single Anthropic key (yours) funds the agent;
  monitoring users never receive a key.
- **Gated and reversible-first.** Risky or irreversible actions require a
  typed human approval; fleet code updates and policy edits *always* need a
  human (policy edits are two-person). The default mode is **shadow** (dry
  run) — actions are graduated to live one at a time after validation.
- **Human authority.** From the dsa110-rt dashboard, humans can lock agents
  out, pin who's in charge, and cap observation time — overrides the agent
  cannot countermand. Plus a console e-stop.
- **Everything logged.** Append-only local JSONL + shared etcd audit trail +
  optional Slack, each line carrying the operator name. No raw telemetry is
  ever sent to the model.

## Quick start

**On your laptop** — one script opens the (self-healing) tunnel and the console:

```bash
python3.10 -m venv .venv && . .venv/bin/activate
pip install -e '.[etcd,web,agent]'
scripts/laptop.sh                          # -> http://127.0.0.1:8787  (no login)
```

That's it — the console is local to your laptop, so there is no login (you are
the operator). Add `ANTHROPIC_API_KEY` to `~/.config/dsa110-operator/secrets.env`
for the real assistant; without it you get the deterministic stub.

**On h23** — one script runs the standing autonomy supervisor (no tunnel):

```bash
scripts/h23_supervisor.sh
```

See [INSTALL](docs/INSTALL.md) for the full setup (secrets, multi-user,
surviving sleep, running as systemd services).

## Layout

| Path | Purpose |
| --- | --- |
| `src/dsa_operator/transport/` | SSH tunnel manager (`-L` forwards through `h23`). |
| `src/dsa_operator/etcd/` | Read-only etcd client over the forwarded port. |
| `src/dsa_operator/tools/` | Typed read-only tool surface the agent may call. |
| `src/dsa_operator/audit/` | Append-only local log + etcd audit + Slack + egress guard. |
| `src/dsa_operator/agent/` | The Claude brain + deterministic stub fallback. |
| `src/dsa_operator/control/` | Single-executor lease, gate engine, approvals, verbs, live executor. |
| `src/dsa_operator/observing/` | Sidereal/transit math, observing-plan model + runner. |
| `src/dsa_operator/monitor/` | Autonomy supervisor (non-LLM health/recovery/injection/plan loops). |
| `src/dsa_operator/web/` | Flask console (local identity, no SSO) + assistant chat. |
| `config/policy.yaml` | Capability policy as code (the approval gates). |
| `config/egress_allowlist.yaml` | The only outbound endpoints permitted. |
| `deploy/` | systemd units + ops runbook. |
