# Using dsa110-operator

Once it's [installed and running](INSTALL.md), open the console at
<http://127.0.0.1:8787>. There is no login — it runs on your own laptop and
you are the operator. This page covers day-to-day use.

For the precise list of what the agent may and may not do, see
[CAPABILITIES](CAPABILITIES.md).

## The console at a glance

Across the **top** is a live **status bar** (auto-refreshing) of the things you
most need at a glance:

| Pill | Shows |
| --- | --- |
| **mode** | `SHADOW (dry-run)` or `LIVE` — whether control actions actually execute (see [Going live](#going-live-flipping-the-policy)). |
| **e-stop** | `clear` or `ENGAGED` (all control halted). |
| **system** | The dashboard's `system_state` (offline / preparing / prepared / ready / observing) and `safe_to_arm`. |
| **antennas** | `settled` or `N moving` (dishes not yet on target). |
| **dec** | The commanded array declination. |
| **plan** | `none`, `staged (not armed)`, or `armed · dec X`. |
| **control** | Whether **you** hold the executor lease, someone else does, or it's free. |

The **assistant chat is always available** in a dock on the right-hand side,
on every tab. Below the status bar are four tabs:

| Tab | What it's for |
| --- | --- |
| **Monitor** | Live state (fleet, pointing, sky, RFI, injections, candidates, audit). |
| **Control** | The executor lease, human-authority/e-stop, proposing control actions, and granting pending approvals. |
| **Plan** | View / stage / preview / arm / advance the observing plan, with example plans to copy. |
| **Autonomy** | The standing supervisor's status and a manual tick. |

## Monitoring and asking questions (anyone)

Any signed-in user can use the **Monitor** tab and chat — no lease required.
Not sure what's available? Ask **"what can you monitor?"** and the agent
enumerates everything (it calls `describe_monitoring`).

**Best first question: "How is the telescope doing?"** — the agent calls
`health_report`, a single ok/warn/alert report card across fleet, pointing,
UDP capture / packet drops, ring buffers, RFI, search, SEFD, injections,
candidates, static sky, and dump state. Drill in from there.

Things you can ask about (one tool each, all read-only):

- **Up & running:** "Are all nodes up?", "Which services failed?", "Is it
  warmed up / safe to arm?"
- **Pointing:** "What's the array pointed at?", "Is dec 69 observable and when
  does it transit?", "Is there an fstable for dec 69?"
- **Data quality:** "Are we dropping packets?", "Ring-buffer pressure?",
  "What's the RFI level (worst nodes)?", "Any cube-dump drops or late
  triggers?"
- **Sensitivity:** "What's the SEFD?", "Current injection K-factor?"
- **Detection chain:** "Did the last injection get detected?", "Recent
  candidates?", "Show the C2 trigger counters."
- **Known sources / pulsars:** "Is B0329+54 transiting soon, and did we detect
  its last transit?" The agent looks up the source's RA/Dec (and DM) itself —
  **there is no catalog** — states the coordinates it used, then calls
  `transit_report`, which tells you the transit time, whether the source is in
  the beam at the current pointing dec, and whether the last transit produced a
  matching candidate (and at what S/N). Compare against the expected S/N to
  gauge sensitivity end-to-end.
- **Config & audit:** "Are voltage dumps enabled?", "Spectral-line mode?",
  "Voltage retention window?", "Who armed the plan?" — and `get_mon` reads any
  raw `/mon/...` key.

The assistant answers by calling the read-only tools; every call is logged
under your operator name. Nothing you do here changes observatory state.

## Taking control (the executor lease)

Only **one** session across all running consoles may execute control
actions, arbitrated by a lease in `h23`'s etcd. In the **Control** tab:

- **Acquire** — take the lease for your session. The header badge switches to
  *EXECUTOR · control enabled*.
- **Release** — give it up so someone else (or the supervisor) can take it.
- **Take over** — seize it from the current holder (audited). Use when the
  holder is gone.

While you don't hold the lease, you can still *ask* the agent to do things —
it will *propose* the action but every mutating call returns `denied`.

## Running control actions

Two ways, both funnelled through the same gates:

**By chat.** Tell the assistant what you want — "point to dec 44", "fire an
injection", "bounce search on the stalled node". It calls `propose_action`
and reports the outcome verbatim: `denied`, `needs_approval`, `shadow` (dry
run), or `executed`. It can *request* an approval but can never grant one.

**By form.** In the **Control** tab, pick an action, supply JSON params, and
click *Propose / run*. The gate (autonomous / approval) is shown next to the
action.

Either way the action only proceeds if it passes the full gauntlet — lease,
dashboard lockout, executor pin, e-stop, gate, parameter validation, and
(for real execution) live mode + promotion. See
[CAPABILITIES §5](CAPABILITIES.md#5-the-safety-gauntlet-every-control-action).

### Approvals

`approval`-gated actions don't run until an authorized human grants them.
Pending requests appear in the **Control** tab with a *Grant* button; grants
are recorded under your operator name and expire after 5 minutes. `set_policy`
needs **two** different approvers.

## Going live: flipping the policy

> **Nothing physically happens until you do this.** Out of the box the system
> is in **shadow** mode, so every control action — including a whole armed
> observing plan — is a **dry run**: it renders the exact etcd writes /
> dashboard POSTs it *would* make and logs them, but **sends nothing**. The
> `mode` pill in the status bar reads `SHADOW (dry-run)`, and the assistant
> says so. This is the safe way to exercise the entire surface first.

Real execution needs **two** switches, and **both** must be set (by design):

1. **Global mode** lives in **`config/policy.yaml`**:

   ```yaml
   mode: shadow        # change to:  live
   ```

2. **Per-action promotion** lives in **`config/local.yaml`** (git-ignored,
   optional). An action executes for real only if it is listed under
   `promote:`. With no file or an empty list, **every** action stays at its
   conservative *commissioning* gate and is a dry-run even in `live` mode.

So: `live` + not-promoted = still a dry-run. `shadow` + promoted = still a
dry-run. You need `mode: live` **and** the action promoted.

### Step by step

1. **Exercise it in shadow first.** Run the action (or arm the plan) and read
   the rendered steps in the output / chat. Confirm the writes/POSTs are what
   you expect. The `mode` pill should say `SHADOW`.

2. **Create your local promotion file** (once):

   ```bash
   cp config/local.yaml.example config/local.yaml
   ```

3. **Promote the validated actions**, one or a few at a time. Edit
   `config/local.yaml` so `promote:` lists them — the example file documents a
   sensible ladder (reversible/low-risk first, fleet-level last). For the
   observing bring-up you need these promoted:

   ```yaml
   promote:
     - point_array
     - build_fstable
     - deploy_fstable
     - set_spectral_line   # only if you use spectral-line mode
     - start_fleet
     - restart_all
     - utc_start
     - utc_stop
   ```

4. **Flip the global mode** when you're ready for real execution — edit
   `config/policy.yaml`:

   ```yaml
   mode: live
   ```

5. **Restart the console** (`scripts/laptop.sh`) so it re-reads the policy.

6. **Verify.** The status-bar `mode` pill should now read `LIVE`. The
   `/api/policy` endpoint (and the **Control** tab's action gates) reflect the
   active mode and promotions. Watch the audit log + Slack as the first real
   actions execute.

### Reverting

To go back to safe dry-runs, set `mode: shadow` in `config/policy.yaml` (or
remove an action from `promote:` to demote just that one) and restart the
console. You can also **engage the e-stop** (Control tab → *Pause*) for an
instant, no-edit halt of all control.

### What you can never promote

`update_fleet_code` and `set_policy` stay `approval` forever — they always need
a human. Editing `config/policy.yaml` itself is the `set_policy` action: it is
**two-person** and done **by hand** (the live executor has no step type that
edits the policy), so changing `mode`/thresholds is always a deliberate manual
edit, never something the agent can do for you.

## Observing plans

DSA-110 is a meridian-transit instrument, so a plan is a timed schedule of
**declinations** (a source is observable around its transit, when LST = RA).
In the **Plan** tab (executor only to change). The tab includes a short
explainer and **example plans** (open-ended dec, transit source, two-dec
survey, spectral-line) you can click to fill the form, edit, and install.

- **Install plan** — provide transit-centred `sources`
  (`{label, ra_deg, dec_deg, window_min}`) or explicit `segments`
  (`{t_start, t_end, dec_deg, label, setup}`). Staged **unarmed** and validated
  against the pointing envelope; nothing moves yet.
- **Preview bring-up** — a dry-run listing of the exact steps the sequencer
  would run for each segment (no change).
- **Arm / Disarm** — a staged plan does nothing until armed; arming is the
  commit after you've confirmed the schedule. Once armed, the **console
  autopilot** advances the bring-up automatically (every ~30 s) for as long as
  your console holds the executor lease — point → fringe-stop table → modes →
  start/restart → wait for warm → `utc_start`.
- **Advance bring-up** — manually step the armed plan's bring-up by **one**
  stage and see the current stage / any blocker. The autopilot normally does
  this for you; use it to nudge or inspect. (This replaces the old "Tick",
  which only nudged *pointing*.)
- **Clear** — remove the active plan.

Every plan-driven action still flows the full gauntlet (lease, e-stop,
dashboard lockout, gate, shadow/live) — arming changes *when* it runs, never
*what is allowed*. In `shadow` mode the autopilot walks the sequence as
dry-runs and sends nothing; see [Going live](#going-live-flipping-the-policy).

You can also just ask the assistant: "set up an observing plan for 3C48 and
3C147" or "what's observable at dec 40 right now?".

## Observing sequences

The assistant can take a plain-language request and run a whole observing
sequence end to end. For example:

> "Observe at DEC 69.04 until nearly an hour before B0329+54 transits, then
> move to the DEC of B0329+54 and observe until an hour after its transit.
> Then move to the DEC of 3C286 and observe until an hour after 3C286
> transits, and return to DEC 69.04 until further instructions."

How it works:

1. **Coordinates.** There is **no built-in source catalog.** The assistant
   looks up each named source's J2000 RA/Dec itself and **states the
   coordinates it is using** so you can verify them.
2. **Schedule.** It computes each source's next transit and turns "an hour
   before/after transit" into explicit times. The final open-ended segment
   ("until further instructions") has no end time.
3. **Confirmation (built in).** It **stages the plan but does not arm it** —
   nothing moves — and shows you the full schedule: every segment's source,
   RA/Dec, dec→el, transit time, exact start/end (move) times, and any per-DEC
   mode. It arms **only after you explicitly confirm.** It does **not** ask
   again before each individual command.
4. **Bring-up per segment.** Once armed, the sequencer runs, for each segment:
   `point_array` (if off target) → wait for the dishes to settle → ensure the
   **fringe-stopping table** exists (`build_fstable` + `deploy_fstable` if
   missing) → apply per-DEC modes → `start_fleet` (or `restart_all` if the
   fleet is already running so it re-reads the new dec/fstable) → wait until
   the fleet reports **warmed** (the dashboard's `system_state` → `prepared` /
   `safe_to_arm`) → `utc_start` (arm recording, holdoff 60000).

To change or stop a running sequence, just say so (the assistant disarms /
clears the plan), or use the dashboard kill switches in
[§ kill switches](#stopping-things-kill-switches).

### Per-DEC modes (spectral line, and beyond)

Each segment can carry a `setup` map of per-DEC mode configuration that is
applied **before the fleet starts** (because those settings take effect at the
next start). Today the built-in mode is spectral line:

```json
{"t_start": ..., "t_end": ..., "dec_deg": 69.04, "label": "HI",
 "setup": {"spectral_line": {"subbands": [3, 4]}}}
```

So you can run different spectral-line configs at different declinations in
one sequence (e.g. line mode at one DEC, continuum at another by omitting it
or setting empty subbands). The mechanism is **extensible**: new per-DEC modes
are added by registering a `ModeApplier` (key → control action) in
`observing/session.py` — `MODE_APPLIERS` / `register_mode_applier` — without
touching the bring-up state machine.

## Autonomy

The **Autonomy** tab shows the standing supervisor — a deterministic
(non-LLM) loop that monitors health, auto-recovers known failures, runs
periodic injection health-checks, and ticks the *armed* observing plan.

- The loops are **on by default** in the shipped policy (toggle them under
  `autonomy:` in `config/policy.yaml`). The health loop runs every 60 s and
  edge-triggers a **Slack alert** when the rolled-up level worsens (e.g. a node
  goes down, RFI spikes, packets start dropping) — wire `DSA_OPERATOR_SLACK_
  WEBHOOK_URL` to receive it.
- Health thresholds are configurable under `autonomy.thresholds`:
  `fleet_min_corr` (16), `fleet_min_search` (4), `rfi_flag_fraction_warn`
  (0.4) / `rfi_flag_fraction_alert` (0.7), `sefd_max_jy` (5000), and the
  sky/SEFD staleness windows.
- Monitoring is read-only. The mutating loops act only when their flag is on
  **and** the supervisor holds the lease **and** agents aren't locked out
  **and** the e-stop is clear — and even then each action runs the full gate
  engine. On a laptop that doesn't hold the lease, the supervisor is purely a
  monitor.
- Run the standing executor on one always-on machine:
  `python -m dsa_operator.monitor.supervisor` (or the systemd unit). It holds
  the lease as session `supervisor`.
- **You don't need that standing process just to run an observation from your
  laptop.** The web console has its own **autopilot**: while your console holds
  the lease and a plan is armed, it drives the bring-up itself (see
  [Observing plans](#observing-plans)). The two never both act — only whoever
  holds the single lease does.

From the web tab you can see status and force a monitor refresh; the mutating
loops stay gated unless the supervisor session holds the lease.

## Stopping things (kill switches)

- **E-stop** (Control tab → *Pause*): your own immediate halt of all control.
  Reads and Q&A keep working. *Resume* to clear it.
- **Dashboard lockout** (on the dsa110-rt dashboard): a human-only override
  the agent cannot countermand — locks agents out entirely, pins who may be
  executor, and caps observation time. The time cap is enforced
  independently by a `dsart_rt` watchdog, so even a runaway agent can't keep
  recording past it.

## Where the logs are

Everything is recorded three ways: an append-only local JSONL audit file (the
system of record, under `DSA_OPERATOR_AUDIT_ROOT`), the shared etcd audit
trail, and — if configured — a Slack summary. Every line carries the operator
name that initiated it.

## After your laptop sleeps

Closing the lid is fine. When you reopen it:

- The **SSH tunnel reconnects on its own** (it self-supervises; under systemd
  `Restart=always` does the same).
- If you **held the executor lease**, it lapsed while you were away (the TTL is
  ~30 s) and control auto-freed on h23. The console shows a banner — click
  **Acquire** (Control tab) to take control again. Check who holds it first;
  someone may have taken over.
- Any **running observation kept going** on the fleet, and its hard time cap is
  enforced on h23, so nothing was stuck or cut short by your laptop.

## Troubleshooting

| Symptom | Likely cause | First action |
| --- | --- | --- |
| Console won't load / read tools error | tunnel not open | start `python -m dsa_operator.transport.ssh_tunnel --ssh-host h23` |
| `etcd3` import error (protobuf) | upstream packaging quirk | prefix commands with `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` |
| "You no longer hold the executor lease" banner | laptop slept / someone took over | re-acquire the lease (Control tab) |
| Chat says "denied" for control | you don't hold the lease, or agents are locked out | acquire the lease (Control tab); check dashboard authority |
| Action returns "shadow" | not promoted / mode is shadow | expected until you go live — see [Going live](#going-live-flipping-the-policy) |
| Armed a plan but nothing moves | mode is shadow (dry-run), or you don't hold the lease | check the `mode` + `control` status-bar pills; go live and acquire the lease. Click **Advance bring-up** (Plan tab) to see the exact stage/blocker. |
| "system_state stuck at ready, safe_to_arm=false" | pipeline not started yet | the bring-up runs `start_fleet` then waits for warm-up; in shadow it never really starts — go live |
| Agent won't act at all | e-stop engaged or dashboard lockout | resume the e-stop; check the dsa110-rt authority panel |
| `corr_fast` stalls after ~N blocks | missing fstable → `meridian_fringestop` crash → buffers back up | build + deploy the fstable for the current dec; restart fleet |
| Injections not detected | noise EMA not converged / wrong apply-at | check warm-up convergence and `apply_at_specnum` |
