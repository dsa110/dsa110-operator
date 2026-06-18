# DSA-110 Operator Agent — Runbook & Capability Contract

> **This document is the human-readable face of `config/policy.yaml`.**
> The gates below are enforced by code; the prose must match the file.
> (A later phase generates the capability tables directly from the policy
> so they can never drift.) **Status: Phase 5** — read-only foundation +
> web console + control gate engine + live executor (graduated per action)
> + observing-plan machinery + the **autonomy supervisor** (standing
> health/recovery/injection/plan loops). Live fires only when
> `mode: live` AND the action is promoted in `config/local.yaml`.

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

### Observing plans (Phase 4)
DSA-110 is a meridian transit instrument, so an observing plan is a timed
schedule of **declinations**; a source is observable around its transit
(when LST == RA). The plan lives in etcd at `/operator/plan/active`
(operator namespace). Endpoints:

* `GET /api/observability?dec=&ra=` — transit elevation, in-envelope?,
  next transit time, current LST (read-only, any user).
* `GET /api/plan` — the active plan + which segment is active now.
* `POST /api/plan` — set a plan (executor only); body is either explicit
  `segments` (`t_start`,`t_end`,`dec_deg`,`label`) or transit-centred
  `sources` (`label`,`ra_deg`,`dec_deg`,`window_min`). Validated against the
  pointing envelope.
* `POST /api/plan/clear` — clear the plan (executor only).
* `POST /api/plan/preview` — what the runner *would* do now (no engine call).
* `POST /api/plan/tick` — run one step: the runner reads the active dec,
  compares to `/mon/array/dec`, and if off-target issues `point_array`
  **through the gate engine** (so during commissioning each plan-driven
  move still needs approval, and nothing moves unless promoted to live).

The agent also has read-only `get_observing_plan` and `get_observability`
tools, so users can discuss the plan and observability conversationally.

## 5. Autonomy supervisor (Phase 5)

A deterministic, non-LLM loop (`src/dsa_operator/monitor/`) provides the
unprompted behaviours. The unit of work is one **tick**; a standing
executor runs the loop with `python -m dsa_operator.monitor.supervisor`
(which acquires the lease as session `supervisor`).

**Loops** (all configured under `autonomy:` in `config/policy.yaml`, all
**off by default**):

| Loop | Flag | What it does |
| --- | --- | --- |
| Health monitor | always on when `enabled` | Polls fleet, static-sky freshness, SEFD freshness, and the observation-time cap; rolls up `ok`/`warn`/`alert` findings; edge-triggered Slack alert on newly-appearing alert codes (no per-tick spam). Read-only. |
| Auto-recovery | `auto_recover` | For known, reversible failure signatures (e.g. search nodes down → `bounce_search`) submits the fix **through the gate engine**. Correlator-down / overrun have no safe auto-remedy and stay alerts. |
| Injection health-check | `injection_health_check` | Fires a synthetic FRB through `fire_injection`, then after `injection_verify_after_s` checks the match count rose — a true end-to-end pulse test. |
| Plan runner | `run_plan` | Ticks the observing-plan runner (§4) on `plan_s` cadence. |

**Gating.** Monitoring is read-only and runs whenever the supervisor is
`enabled`. The three mutating loops act only when their flag is set **and**
this session holds the lease **and** `agents_enabled` (dashboard) **and**
the e-stop is clear — and even then every action runs the full gate engine,
so during commissioning a "recovery" or plan move surfaces as
`needs_approval` and is logged, not executed.

Endpoints: `GET /api/autonomy` (status: enabled flags, cadences, active
alerts, last tick) and `POST /api/autonomy/tick` (force a monitor refresh;
from the web the mutating loops stay gated unless the supervisor session
holds the lease). Everything the supervisor does is audited.

## 6. Troubleshooting playbook (seeded; expand as we learn)

| Symptom | Likely cause | First action |
| --- | --- | --- |
| `corr_fast` stalls after ~N blocks | missing fstable → `meridian_fringestop` crash → `bada`/`fada` back-up | check fstable for current dec; build+deploy; restart fleet |
| C1 emit wedged on a search node | search ring stall | `bounce_search` on that cn |
| Buffers fill on start | stale `replay_voltage_dump` holding a buffer | kill stale process on the corr node |
| Injections not detected | warm-up not converged / wrong apply-at | check noise EMA convergence; verify `apply_at_specnum` |

## 6b. Human authority from the dsa110-rt dashboard

Humans hold a one-way override the agent **cannot** countermand, asserted
through a single etcd key, `/cmd/operator/control`, written only from the
dsa110-rt dashboard. That key sits outside every prefix the operator can
write (`/operator/` and `/cmd/ant/`), so the agent can read it but never
enable itself, re-point the executor, or extend its own time limit.

| Field | Effect |
| --- | --- |
| `agents_enabled: false` | **Lockout.** Every agent control attempt fails closed (reads/Q&A continue). |
| `executor_email: <addr>` | **Pin who's in charge.** Only that Google identity may hold the lease and act; unset ⇒ operator self-arbitrates. |
| `max_obs_seconds: <n>` | **Hard observation cap.** The watchdog (and, ideally, a dsart-side watchdog) stops recording after this long. |

Operator side (this build): `GET /api/authority` shows the asserted
authority; `GET /api/observation` shows armed/elapsed/overrun vs the cap;
lease acquisition and every control attempt honour the lockout and pin.

The **truly strict** time limit is enforced independently of the agent by a
watchdog inside `dsart_rt`: `utc_start` records when this orchestrator armed
recording, and the mon loop auto-`utc_stop`s once the elapsed time exceeds
`/cmd/operator/control.max_obs_seconds` (cached ~15 s, fail-open, no-op when
unset). So even a runaway or crashed agent cannot exceed the cap. The
dashboard UI to set these fields and the watchdog ship on the dsa110-rt
`operator-integration` branch.

## 6c. The Claude account / API key

The brain is one server process holding **one** Anthropic API key.
Monitoring users never receive a key: they sign in to the single console
via Google SSO, and that process makes the Anthropic calls. Provision the
key out-of-band into the server's environment (`ANTHROPIC_API_KEY`), or a
git-ignored `~/.config/dsa110-operator/secrets.env` / repo `.env` (see
`.env.example`). Prefer a dedicated Anthropic workspace key with a spend
cap. Nothing secret is logged or committed; without a key the console
falls back to the stub agent.

## 7. Control protocol

* **Lease / takeover:** the executor holds a short-TTL etcd lease; others
  are read-only until release/expiry. Takeover is explicit and audited.
* **Approvals:** requested in the web console (and pinged to Slack),
  granted by an authorized human, expire after `approval.ttl_seconds`.
* **Logging:** every action → append-only local JSONL (system of record)
  + the shared etcd `/mon/audit/...` trail + a Slack summary.
* **E-stop:** set `paused: true` (console/CLI) to halt all control — this
  is the operator's *own* stop. The dashboard `agents_enabled: false`
  lockout is the stronger, human-only override the agent cannot clear.

## 8. Roadmap

Phase 0 read-only foundation → 1 web + SSO + multi-user → **2 lease +
policy engine + shadow mode (done)** → **3 graduated live control + live
executor (done)** → **4 pointing helpers + observing plan + runner
(done)** → **5 autonomy: standing health/recovery/injection loops + the
runner on a cadence, all gated through the engine (done)**. Next:
graduate actions to live via `promote:` as each is validated, and expand
the recovery playbook + health thresholds from operational experience.

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
