# What the operator agent can and cannot do

This is the human-readable face of `config/policy.yaml` — the single source
of truth. If this page and that file ever disagree, **the file wins** (it is
what the code enforces).

Two ideas govern everything:

- **Who** is acting — a *monitoring* user (anyone with a console open) or the
  one *executor* (the single session holding the lease).
- **What gate** an action has — `autonomous`, `approval`, or `forbidden` —
  and whether the system is in `shadow` (dry-run) or `live` mode.

---

## 1. Anyone with a console (read-only, no lease)

Each person runs the console locally (no login); anyone can read state and ask
the assistant questions. These never change anything. Ask the agent **"what can
you monitor?"** (or call `describe_monitoring`) for the live list. By category:

| Category | Tools | What you can ask |
| --- | --- | --- |
| Alive & running | `get_fleet_status`, `get_services_status`, `get_warmup_status` | Which nodes are up? Is the fleet observing / safe to arm? Is it warmed up? |
| Pointing | `get_array_pointing`, `get_observability`, `get_observing_plan`, `get_fstable_status` | Where is the array pointing? Is dec X observable, and when does it transit? Is there an fstable for dec X? |
| Data quality | `get_capture_health`, `get_buffer_health`, `get_rfi_summary`, `get_rfi_detail`, `get_search_health` | Are we dropping packets? Ring-buffer pressure? RFI level? Search compute/noise/cube-dump health? |
| Sensitivity | `get_sefd`, `get_inject_calibrations` | What's the SEFD / coherence? Current K-factor calibration? |
| Detection chain | `query_injections`, `transit_report`, `list_candidates`, `get_candidate`, `get_c2_status`, `get_sky_status` | Are injections detected? Did pulsar/calibrator X transit and get detected? Recent candidates? C2 triggers? Static-sky image fresh? |
| Config & audit | `get_dumps_state`, `get_spectral_line_state`, `get_voltage_retention`, `get_audit_log`, `get_mon` | Are dumps enabled? Spectral-line mode? Voltage retention window? Who did what? Any `/mon/...` key. |
| Rollup | `health_report` | "How is the telescope doing?" — one ok/warn/alert report card across all of the above. |

**Pulsars / known sources — no catalog.** When you ask about a pulsar or
calibrator, the agent looks up its J2000 RA/Dec (and DM / expected flux) from
its own knowledge, states the values it used, and feeds them to
`transit_report`, which reports the transit time, whether the source is in the
beam at the current pointing dec, and whether the *last* transit produced a
matching detection (and at what S/N). This is a strong end-to-end check.

## 2. The executor (lease holder) — control actions

The single lease-holding session can run the actions below. All of these are
**autonomous**: the executor's agent may run them without a per-action human
OK (still audited and Slack-notified). The two exceptions that always need a
human are in §3.

In the **Control** tab you pick the action and supply the **parameters** as a
JSON object; in chat the assistant fills them in for you. Parameters in **bold**
are required; the rest are optional (defaults noted).

| Action | Parameters (JSON) | Reversible | What it does |
| --- | --- | --- | --- |
| `point_array` | **`dec_deg`** (number); `refants` (list of ant ids) | yes | Slew dishes in elevation for a transit pointing (`el = 90 − (lat − dec)`). Refused if dec → el falls outside the `[el_min, el_max]` envelope. |
| `start_fleet` | `dec_deg` (number) | yes | Start the pipeline fleet (optionally for a given observing dec). |
| `stop_fleet` | — | yes | Stop the pipeline fleet. |
| `restart_all` | `dec_deg` (number) | yes | Cold fleet restart (async) so a new dec/fstable is picked up. |
| `bounce_search` | `cn_ids` (list of node ids) | yes | Restart the search half (all search nodes, or just `cn_ids`). |
| `build_fstable` | **`dec_deg`** (number); `force` (bool) | yes | Build a fringe-stopping table for a dec (cache file; no observing impact). |
| `deploy_fstable` | **`filename`** (bare `.npz` basename, no `/`) | yes | Push a built fringe-stop table to the correlator nodes. |
| `utc_start` | `margin` (int ms, default `30000`) | yes | Arm recording (computes ARM_SEQ from capture). The observing sequencer uses `margin=60000`. |
| `utc_stop` | — | yes | Stop / disarm recording. |
| `set_spectral_line` | **`subbands`** (list, or `[]`/omit for continuum); `reason` (string) | no | Set spectral-line mode; takes effect at the **next** fleet start. |
| `set_dumps_enabled` | **`enabled`** (bool); `reason` (string) | yes | C2 voltage-dump enable/disable kill-switch. |
| `dump_now` | — | yes | Trigger an immediate voltage dump. |
| `fire_injection` | `target_snr`, `dm_pc_cm3`, `l_rad`, `m_rad`, `width_samples`, `fluence_jy_ms`, `profile`, `chgroups`, `margin_blocks` (all optional) | yes | Synthetic FRB into `corr_fast`; health-check + calibration. |
| `inject_calibrate` | `dm_pc_cm3`, `l_rad`, `m_rad`, `width_samples`, `fluence_jy_ms`, `profile`, `chgroups`, `use_ladder`, `fluence_ladder`, `health_check` (all optional) | yes | Injection-based SNR (K-factor) calibration. |
| `delete_snr_cal` | — | no | Delete the stored SNR calibration. |

> **Per-action approvals are not required for these.** The agent's safeguard
> for multi-step observing is **plan-level confirmation**: before it executes
> a setup or a sequence, it presents the full schedule (sources, coordinates,
> dec→el, transit times, exact move times) and waits for your explicit
> go-ahead — see [USAGE](USAGE.md#observing-sequences) and §5. It does not
> ask again before each individual command.

### 2.1 Observing-plan tools (what the assistant drives in chat)

You rarely call the raw actions above for observing — you describe what you
want and the assistant composes them into a plan. These are the chat tools it
uses (all bound to your identity + session and gated exactly like the actions
above):

| Tool | Parameters | What it does |
| --- | --- | --- |
| `observe_at_dec` | **`dec_deg`**; `start_unix`, `end_unix` (omit for open-ended), `label`, `note` | Stage a single-segment plan at one dec (UNARMED). |
| `set_observing_plan` | `sources` (`[{label, ra_deg, dec_deg, window_min}]`) **or** `segments` (`[{t_start, t_end, dec_deg, label, setup}]`); `note` | Stage a full plan (UNARMED). Validated against the elevation envelope. |
| `compute_transits` | `sources` (`[{label, ra_deg, dec_deg}]`) | Next transit time, dec→el, observability — used to lay out a schedule. |
| `preview_observing_plan` | — | Dry-run of the exact bring-up steps per segment (no change). |
| `arm_observing_plan` | — | Commit the staged plan. After this the bring-up runs automatically (see §7). |
| `disarm_observing_plan` / `clear_plan` | — | Stop acting on / delete the plan. |
| `observing_status` | — | Is a plan armed? which segment is active now? |
| `run_observing_step` | — | Advance the armed bring-up one step and report the current stage / blocker. |
| `lease_status` / `list_control_actions` | — | Who holds control / the available actions and their gates. |
| `propose_action` / `request_approval` | `action`, `params` | Run any §2 action through the gate engine / request a human approval. |

A "spectral line OFF / continuum" request just omits `setup`; to configure
spectral line at a dec, put it in that segment's `setup`, e.g.
`setup={"spectral_line": {"subbands": [3, 4]}}`.

## 3. Always require a human (never autonomous)

These stay `approval` in **both** columns — they cannot be graduated:

| Action | Parameters | Reversible | Why |
| --- | --- | --- | --- |
| `update_fleet_code` | `branch`, `hosts` | no | `git pull`/reset across the fleet. **Always** a human. |
| `set_policy` | (edited by hand) | no | Edits this policy. **Two-person**, and performed by hand — the live executor cannot do it (see §8). |

## 4. What it can never do

- **Reach any host but `h23`.** etcd, the dashboard, and data are all reached
  through the one SSH hop. No other outbound network except the allowlist
  (Anthropic API, Slack) in `config/egress_allowlist.yaml`.
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
| **Pin the executor** (`executor_email`) | Only that operator name may hold the lease and act. |
| **Observation time cap** (`max_obs_seconds`) | A `dsart_rt` watchdog auto-stops recording after this long, regardless of the agent. |

Plus the operator's own **e-stop** (`paused`) inside this console, which
halts all control immediately.

## 7. Autonomy (unprompted behaviour)

A separate, deterministic (non-LLM) supervisor runs standing loops — health
monitoring (every 60 s), auto-recovery of known failures, periodic injection
health-checks (hourly), and ticking the *armed* observing plan. These loops
are **on by default** in the shipped policy. The health loop is read-only and
edge-triggers a Slack alert when the report level worsens. The mutating loops
(auto-recover / injection / plan) act only when their flag is on **and** the
supervisor holds the lease **and** agents aren't locked out **and** the e-stop
is clear — and even then each action runs the full gauntlet above, so on a
laptop without the lease the supervisor is effectively a monitor. Health
thresholds (fleet counts, RFI flag-fraction warn/alert, SEFD ceiling, sky/SEFD
staleness) live under `autonomy.thresholds` in `config/policy.yaml`. See
[USAGE](USAGE.md#autonomy).

**The console autopilot.** Arming a plan only records intent — *something* has
to step the bring-up (point → fstable → modes → start/restart → warm →
`utc_start`). The standing supervisor does this when it holds the lease. The
web console also has its own autopilot: while **your** console holds the
executor lease and a plan is armed, it advances the bring-up on the plan
cadence, acting as the lease holder. It stays idle when another process (e.g.
the h23 supervisor) holds the lease, so only the single lease holder ever
drives. Every step still runs the full gauntlet — including shadow/live — so
in `shadow` mode the autopilot walks the sequence as dry-runs and **nothing is
sent**. (This is why "arm the plan" does nothing physical until you go live;
see §8.)

## 8. Changing what's allowed: shadow → live, and promotion

Two independent switches decide whether a control action *actually executes*,
and **both** must be set (this is by design — see the gauntlet in §5):

1. **Global mode** — `mode:` in `config/policy.yaml`. `shadow` (the shipped
   default) renders the exact writes/calls each action would make and logs
   them **without sending**; `live` lets execution through.
2. **Per-action promotion** — the `promote:` list in `config/local.yaml`
   (git-ignored, optional). An action runs live only if it is promoted. With
   no file / empty list, **every** action stays at its conservative
   *commissioning* gate and never executes for real, even in `live` mode.

So `live` + not-promoted = still a dry-run; `shadow` + promoted = still a
dry-run. You need `mode: live` **and** the action in `promote:`.

`update_fleet_code` and `set_policy` can **never** be promoted — they always
need a human, and `set_policy` (editing this file) is two-person and is done by
hand, since the live executor has no step type that edits the policy.

The exact, step-by-step procedure (with file snippets and how to verify and
revert) is in [USAGE → Going live](USAGE.md#going-live-flipping-the-policy).
