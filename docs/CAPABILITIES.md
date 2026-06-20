# What the operator agent can and cannot do

This is the human-readable face of `config/policy.yaml` — the single source
of truth. If this page and that file ever disagree, **the file wins** (it is
what the code enforces).

Two ideas govern everything:

- **Who** is acting — a *monitoring* user (any signed-in person) or the one
  *executor* (the single session holding the lease).
- **What gate** an action has — `autonomous`, `approval`, or `forbidden` —
  and whether the system is in `shadow` (dry-run) or `live` mode.

---

## 1. Anyone signed in (read-only, no lease)

Every authenticated Google-SSO user can read state and ask the assistant
questions. These never change anything:

`get_fleet_status`, `get_array_pointing`, `get_mon`, `get_audit_log`,
`list_candidates`, `get_candidate`, `get_sefd`, `get_rfi_summary`,
`get_sky_status`, `query_injections`, `get_observing_plan`,
`get_observability`.

## 2. The executor (lease holder) — control actions

The single lease-holding session can run the actions below. All of these are
**autonomous**: the executor's agent may run them without a per-action human
OK (still audited and Slack-notified). The two exceptions that always need a
human are in §3.

| Action | Gate | Reversible | What it does |
| --- | --- | --- | --- |
| `fire_injection` | autonomous | yes | Synthetic FRB into `corr_fast`; health-check + calibration |
| `inject_calibrate` | autonomous | yes | Injection-based SNR calibration |
| `set_dumps_enabled` | autonomous | yes | C2 voltage-dump enable/disable kill-switch |
| `dump_now` | autonomous | yes | Trigger an immediate dump |
| `build_fstable` | autonomous | yes | Build a fringe-stopping table (cache file; no observing impact) |
| `deploy_fstable` | autonomous | yes | Push fringe-stop tables to correlator nodes |
| `point_array` | autonomous | yes | Slew dishes in elevation (dec → el) |
| `start_fleet` | autonomous | yes | Start the pipeline fleet |
| `stop_fleet` | autonomous | yes | Stop the pipeline fleet |
| `bounce_search` | autonomous | yes | Restart search on a node |
| `utc_start` | autonomous | yes | Arm recording (ARM_SEQ) |
| `utc_stop` | autonomous | yes | Stop recording |
| `set_spectral_line` | autonomous | no | Spectral-line mode (takes effect next fleet start) |
| `delete_snr_cal` | autonomous | no | Delete an SNR calibration file |

> **Per-action approvals are not required for these.** The agent's safeguard
> for multi-step observing is **plan-level confirmation**: before it executes
> a setup or a sequence, it presents the full schedule (sources, coordinates,
> dec→el, transit times, exact move times) and waits for your explicit
> go-ahead — see [USAGE](USAGE.md#observing-sequences) and §5. It does not
> ask again before each individual command.

## 3. Always require a human (never autonomous)

These stay `approval` in **both** columns — they cannot be graduated:

| Action | Reversible | Why |
| --- | --- | --- |
| `update_fleet_code` | no | `git pull`/reset across the fleet. **Always** a human. |
| `set_policy` | no | Edits this policy. **Two-person**: needs a second authorized approver. |

## 4. What it can never do

- **Reach any host but `h23`.** etcd, the dashboard, and data are all reached
  through the one SSH hop. No other outbound network except the allowlist
  (Anthropic API, Google OAuth, Slack) in `config/egress_allowlist.yaml`.
- **Touch the `lxd110h20` web UI.** By design, never.
- **Run a raw shell or write an arbitrary etcd key.** Only the typed,
  allow-listed actions above exist; the model never holds a raw client. The
  only etcd control key the executor writes directly is `/cmd/ant/<n>`
  (antenna elevation, for pointing).
- **Act without the lease.** Non-executor sessions can *ask* and *propose*,
  but every mutating call returns `denied`.
- **Self-approve.** The agent can *request* an approval; only a human can
  *grant* it.
- **Override the human authority keys** (`agents_enabled`, `executor_email`,
  `max_obs_seconds`) — those live outside every prefix it can write.
- **Exceed the observation time cap.** Enforced independently by a watchdog
  in `dsart_rt`, so even a runaway agent cannot keep recording past the cap.
- **Act while paused** (e-stop): reads and Q&A continue; every mutating tool
  fails closed.

## 5. The safety gauntlet (every control action)

Even when the agent proposes an action, it only happens if it passes **all**
of these, in order:

```
lease held?  →  agents_enabled (dashboard)?  →  executor pin matches?  →
e-stop clear?  →  action's gate (autonomous / approval granted)?  →
parameters valid?  →  mode live AND action promoted?  →  EXECUTE
```

If any check fails, the action is denied or held for approval, and the
attempt is logged. In `shadow` mode (the default) the final step renders the
exact writes/calls it *would* make and logs them — **without** sending.

## 6. The kill switches (human authority)

Three controls live on the **dsa110-rt dashboard** (not in this app), in a
single etcd key the agent can read but never write:

| Control | Effect |
| --- | --- |
| **Lock out agents** (`agents_enabled: false`) | Every agent control attempt fails closed. Reads/Q&A keep working. |
| **Pin the executor** (`executor_email`) | Only that Google identity may hold the lease and act. |
| **Observation time cap** (`max_obs_seconds`) | A `dsart_rt` watchdog auto-stops recording after this long, regardless of the agent. |

Plus the operator's own **e-stop** (`paused`) inside this console, which
halts all control immediately.

## 7. Autonomy (unprompted behaviour)

A separate, deterministic (non-LLM) supervisor can run standing loops —
health monitoring, auto-recovery of known failures, periodic injection
health-checks, and ticking the observing plan. **Every loop is off by
default.** Monitoring is read-only; the mutating loops act only when their
flag is on **and** the supervisor holds the lease **and** agents aren't
locked out **and** the e-stop is clear — and even then each action runs the
full gauntlet above. See [USAGE](USAGE.md#autonomy).
