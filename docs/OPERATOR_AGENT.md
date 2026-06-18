# DSA-110 Operator Agent — Runbook & Capability Contract

> **This document is the human-readable face of `config/policy.yaml`.**
> The gates below are enforced by code; the prose must match the file.
> (A later phase generates the capability tables directly from the policy
> so they can never drift.) **Status: Phase 3** — read-only foundation +
> web console + control gate engine + a **live executor graduated per
> action**. Live fires only when `mode: live` AND the action is promoted
> in `config/local.yaml`; defaults stay shadow.

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
human-approved) fleet code updates.

Each verb runs the full gauntlet — lease → e-stop → gate → approval →
parameter validation — and then either renders the exact writes it would
make (shadow) or performs them (live). Console endpoints: `POST
/api/control`, `GET /api/policy`, `GET/POST /api/lease[...]`, `GET/POST
/api/approvals[...]`, `POST /api/pause`, `POST /api/resume`.

**How live execution is reached (Phase 3).** The engine executes for real
only when ALL of these hold: a live executor is wired (it is, by default,
on the laptop), the policy is `mode: live`, AND the action is listed under
`promote:` in `config/local.yaml`. Graduation is therefore strictly
per-action and explicit. The shipped defaults (`mode: shadow`, no
`local.yaml`) keep every path a dry run.

**What the executor can touch.** It delegates to the observatory's own
audited dashboard routes for almost everything — `start`/`stop`,
`utc_start`/`utc_stop`, `dumps_enabled`, `dump_now`, `inject`,
`inject_calibrate`, `fstables/build`/`deploy`, `spectral_line`,
`bounce_search`, `delete_snr_cal`, `update_dsart` — reusing their
ARM_SEQ/ssh/UDP/rsync/K-calibration logic. The **only** etcd control key
it writes directly is `/cmd/ant/<n>` (antenna elevation for pointing),
allow-listed in the executor. It never runs a raw shell, never writes an
arbitrary etcd key, and cannot edit its own policy (`set_policy` is a
manual, two-person, by-hand action). The e-stop (`paused`) blocks even
promoted, live actions.

### Web console (Phase 1)
A Flask app on the laptop, behind **Google SSO**. Every authenticated user
gets the read-only views and an **assistant chat** that answers questions
by calling the read-only tools above (each call audited under that user's
Google identity). The brain is Claude via the operator's own Anthropic
account; when no API key is present it falls back to a deterministic
keyword-routed stub so monitoring/Q&A still works.

Run it:

```bash
pip install -e '.[web,agent]'              # agent extra optional
# open the tunnel in another shell: python -m dsa_operator.transport.ssh_tunnel
export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=...
export DSA_OPERATOR_ALLOWED_DOMAINS=dsa110.org      # or _ALLOWED_EMAILS=a@x,b@y
export DSA_OPERATOR_REDIRECT_URI=http://localhost:8787/auth/callback
export ANTHROPIC_API_KEY=sk-ant-...                 # optional; stub used if unset
python -m dsa_operator.web.app                      # serves 127.0.0.1:8787
```

Authorization is an allowlist (`DSA_OPERATOR_ALLOWED_DOMAINS` /
`DSA_OPERATOR_ALLOWED_EMAILS`); unlisted Google accounts are denied and the
denial is audited. The session cookie is HTTP-only, SameSite=Lax, signed
with `DSA_OPERATOR_SECRET_KEY`. The console exposes **no mutating routes**
other than `chat`/`login`/`logout` (enforced by a test).

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

Phase 0 read-only foundation → 1 web + SSO + multi-user → **2 lease +
policy engine + shadow mode (done)** → **3 graduated live control + live
executor (done)** → 4 pointing helpers + conversational observing plan →
5 autonomy + monitor/recovery loops. See the team chat design for detail.

Promotion to live is per-action: list a validated action under `promote:`
in `config/local.yaml` to move it from its `commissioning` gate to its
`target` gate and enable real execution for it. Until then every control
path is exercised end-to-end without touching the array.

**dsa110-rt:** Phase 3 required **no** dsa110-rt changes — the executor
reuses the existing `dsa_monitor` `/control/` routes and the `/cmd/ant`
etcd convention. The one open integration item (a read-only operator-lease
panel on the dsa110-rt dashboard so humans see who holds operator control,
and optionally making the dashboard's control routes lease-aware) is left
for a dedicated dsa110-rt branch, to avoid colliding with concurrent work
there.
