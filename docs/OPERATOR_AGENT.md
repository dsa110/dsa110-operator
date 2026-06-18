# DSA-110 Operator Agent — Runbook & Capability Contract

> **This document is the human-readable face of `config/policy.yaml`.**
> The gates below are enforced by code; the prose must match the file.
> (A later phase generates the capability tables directly from the policy
> so they can never drift.) **Status: Phase 0** — read-only foundation.

## 1. What the agent is

An assistant, powered by Claude, that monitors and (in later phases)
controls the DSA-110 real-time system. It runs on an operator laptop and
reaches the observatory **only over SSH to `h23`**. It does **not** touch
the `lxd110h20` web UI. Many people can connect to **watch and ask
questions**; only **one** session at a time may **execute control
actions**, arbitrated by an etcd lease.

## 2. How it connects (and what it may contact)

```
laptop ──ssh──> h23 ──┬── etcd (etcdv3service.pro.pvt:2379)   [forwarded :12379]
                      ├── dsa_monitor dashboard (:5778)        [forwarded :15778]
                      └── data products on /dataz             [sftp/rsync over ssh]
```

Outbound network is restricted to the allowlist in
`config/egress_allowlist.yaml`: **Anthropic API**, **Google OAuth**,
**Slack webhook**, and **SSH to `h23`**. Nothing else.

**No raw telemetry is sent to the model.** Local code summarises `/mon`
data; only compact, secret-redacted summaries enter the Claude context.

## 3. What it CAN do

### Read-only (any authenticated Google SSO user, no lease)
`get_fleet_status`, `get_array_pointing`, `get_mon`, `get_audit_log`,
`list_candidates`, `get_candidate`, `get_sefd`, `get_rfi_summary`,
`get_sky_status`, `query_injections`.

### Control (lease holder only; gated by `config/policy.yaml`)
Pointing the array (dec → elevation), starting/stopping/reconfiguring
observing, arming/disarming recording, firing injections + calibration,
C2 dump controls, fringe-stop table build/deploy, and (always
human-approved) fleet code updates. **Not present in Phase 0.**

## 4. What it CANNOT do

* Contact any host but `h23`, or use the `lxd110h20` web UI.
* Run a raw shell or issue a raw etcd write — only typed, allow-listed
  tools exist; the model never holds a raw client.
* Execute control without holding the single-executor lease.
* Perform an `approval`-gated action without a human's typed, SSO-bound,
  time-limited confirmation. **Updating fleet code and editing the policy
  always require a human** (policy edits are two-person).
* Act at all while globally **paused** (the e-stop): reads/Q&A continue,
  every mutating tool fails closed.

## 5. Required monitoring (Phase 5 target)

Continuously: fleet heartbeats (`/mon/service/*`), buffer health, RFI,
SEFDs, sky-monitor freshness, C2 liveness, and injection match rate.
Periodically fire injection health-checks. Alert to Slack on threshold
breaches. Cadences/thresholds are configured alongside the monitor loop.

## 6. Troubleshooting playbook (seeded; expand as we learn)

| Symptom | Likely cause | First action |
| --- | --- | --- |
| `corr_fast` stalls after ~N blocks | missing fstable → `meridian_fringestop` crash → `bada`/`fada` back-up | check fstable for current dec; build+deploy; restart fleet |
| C1 emit wedged on a search node | search ring stall | `bounce_search` on that cn |
| Buffers fill on start | stale `replay_voltage_dump` holding a buffer | kill stale process on the corr node |
| Injections not detected | warm-up not converged / wrong apply-at | check noise EMA convergence; verify `apply_at_specnum` |

## 7. Control protocol

* **Lease / takeover:** the executor holds a short-TTL etcd lease; others
  are read-only until release/expiry. Takeover is explicit and audited.
* **Approvals:** requested in the web console (and pinged to Slack),
  granted by an authorized human, expire after `approval.ttl_seconds`.
* **Logging:** every action → append-only local JSONL (system of record)
  + the shared etcd `/mon/audit/...` trail + a Slack summary.
* **E-stop:** set `paused: true` (console/CLI) to halt all control.

## 8. Roadmap

Phase 0 read-only foundation → 1 web + SSO + multi-user → 2 lease +
policy engine + shadow mode → 3 graduated live control → 4 pointing +
observing plan → 5 autonomy + monitor/recovery. See the team chat design
for detail.
