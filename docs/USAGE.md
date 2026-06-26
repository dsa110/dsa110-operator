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

### Preflight: "can it actually start right now?"

Before arming a plan — or any time an armed plan seems to do nothing — run the
**preflight** check. It validates every precondition the bring-up depends on
(mode, the executor lease, the e-stop, the dashboard lockout/pin, a wired live
executor, and that each bring-up action is promoted) and prints a concrete
blocker list with the fix for each.

```bash
# Fast, offline: validates config/policy.yaml + config/local.yaml only.
python -m dsa_operator.preflight --no-etcd

# Full check (run with the SSH tunnel up): also checks the live lease,
# e-stop, and dashboard authority.
python -m dsa_operator.preflight          #  --json for machine-readable output
```

`scripts/laptop.sh` runs the offline preflight automatically at startup, so a
broken `config/local.yaml` or an empty `promote:` list is caught loudly instead
of degrading every action to a silent dry-run. The assistant has the same check
as a **`preflight`** tool and reports `ready_to_observe` plus the blockers; it
also receives a per-turn *live situation* snapshot (mode/lease/e-stop/plan), so
asking it "why is nothing happening?" gets a precise answer rather than a guess.

> **In live mode the sequencer now refuses to fake success.** If a bring-up
> step comes back `shadow` because it isn't promoted (with a real executor
> wired), the sequencer **blocks** on that step — e.g. `restart_all → shadow:
> ... not promoted` — instead of marching to `done` while the array never
> moves. Preflight catches this before you arm.

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

## Setting up the laptop and h23

There are two places the operator runs, each from **its own clone** of this
repo. They share state through `h23`'s etcd (the lease, the armed plan, the
audit trail), but they read their **own local files** for configuration.

### Which config files matter, and where

| File | Tracked by git? | Scope | What it controls |
| --- | --- | --- | --- |
| `config/policy.yaml` | **yes** (committed) | the same on every clone after a `git pull` | global `mode: shadow\|live`, the gate for each action, the `observing` arm-on-dec-ready override, and the `autonomy:` loop switches |
| `config/local.yaml` | **no** (git-ignored) | **per-machine** — exists independently on the laptop and on h23 | the `promote:` list: which actions are allowed to execute for real (vs dry-run) on *that* machine |

**The key point:** because `config/local.yaml` is git-ignored, it does **not**
travel with `git pull`. Each machine needs its own copy. If h23 has no
`config/local.yaml` (or an empty `promote:`), then **every** action the h23
supervisor runs is a **shadow dry-run** even with `mode: live` — an armed plan
handed off to h23 will "run" but move nothing. This is a common cause of
"schedules don't do anything on h23". Create the file on **both** machines.

> Do I edit both `policy.yaml` *and* `local.yaml` on both machines?
> - **`policy.yaml`:** edit it **once**, commit, and `git pull` on the other
>   machine. You don't maintain two different copies — both clones should be on
>   the same commit so they enforce the same gates/mode. (It's read from each
>   clone's own working tree, so the pull is what syncs it.)
> - **`local.yaml`:** **yes — create/maintain it on each machine**, because
>   it's git-ignored. Keep the `promote:` lists identical on the laptop and h23
>   so an observation behaves the same whoever holds the lease.

### Laptop (the console + assistant)

1. Clone the repo and install the runtime deps into your env (a conda env is
   typical): `pip install etcd3 pyyaml requests anthropic`.
2. Put secrets in `scripts/.env` (or `~/.config/dsa110-operator/secrets.env`):
   `ANTHROPIC_API_KEY=...` (for the real assistant) and optionally
   `DSA_OPERATOR_SLACK_WEBHOOK_URL=...`.
3. Create `config/local.yaml` with the actions you've validated (see
   [Going live](#going-live-flipping-the-policy) for the recommended list).
4. Confirm `ssh h23` works non-interactively (passwordless key + `~/.ssh/config`
   `Host h23` entry).
5. Start it: `scripts/laptop.sh`. This opens the self-healing SSH tunnel
   (etcd + dashboard) and serves the console at <http://127.0.0.1:8787>. The
   laptop reaches etcd/dashboard **through the tunnel** (loopback ports), which
   the defaults already assume.
   - To run it as a managed service instead: `scripts/install_service.sh laptop
     --enable` (installs the tunnel + web units).

### h23 (the standing executor)

This is the always-on process that keeps an armed plan running after you walk
away. Run **at most one** across the whole site.

1. Clone the repo on h23 and install the deps into the env you'll run it from:
   `pip install etcd3 pyyaml requests` (no Anthropic key needed — the
   supervisor is deterministic, non-LLM).
2. `git pull` so h23 has the **same `config/policy.yaml`** as your laptop
   (same `mode`, gates, autonomy switches).
3. **Create `config/local.yaml` on h23** with the **same `promote:` list** as
   the laptop. *(This is the easy step to forget — without it h23 dry-runs
   everything.)*
4. Install + start the service: `scripts/install_service.sh h23 --enable`, then
   `loginctl enable-linger "$USER"` so it survives logout. On h23 it talks to
   etcd and the dashboard **directly** (no tunnel) — the systemd unit bakes in
   `DSA_OPERATOR_ETCD_HOST=etcdv3service.pro.pvt` and
   `DSA_OPERATOR_DASHBOARD_PORT=5778`.
5. Verify: `systemctl --user status dsa110-operator-supervisor-h23` and watch
   its audit at `~/.local/share/dsa110-operator/audit/`.

The supervisor starts **monitor-only** if your laptop currently holds the
lease, and **reclaims the lease automatically** the moment you release it (or
your laptop sleeps), continuing the same armed plan — see
[Handing a running schedule off to h23](#handing-a-running-schedule-off-to-h23).

### One-time go-live checklist (both machines)

- [ ] `config/policy.yaml` → `mode: live` (committed + pulled on both)
- [ ] `config/local.yaml` exists on **the laptop** with the bring-up actions promoted
- [ ] `config/local.yaml` exists on **h23** with the **same** promote list
- [ ] both clones on the same git commit
- [ ] `python -m dsa_operator.preflight --no-etcd` reports **READY** on both machines
- [ ] laptop console restarted / h23 service restarted so they re-read policy
- [ ] status-bar `mode` pill reads **LIVE**; run one action and confirm it shows a green `live` row (not yellow `shadow`) in the Activity feed

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

#### Arming when safe_to_arm stays false

The warm step normally waits for the dashboard to report `system_state` →
`prepared` / `safe_to_arm`. On a healthy array that flag can stay `false` for a
while — e.g. a couple of dishes are still settling after a slew — which stalls
the bring-up at the **warm** stage until it eventually times out.

To handle that, the sequencer has an **operator-controlled override** in
`config/policy.yaml`:

```yaml
observing:
  arm_on_dec_ready: true     # arm even if safe_to_arm is false, provided...
  max_moving_antennas: 4     # ...the array is on target and ≤ this many move
```

When `arm_on_dec_ready` is on, both the **settle** and **warm** waits also pass
if the DEC/pointing service reports the array **on target** and **at most
`max_moving_antennas` dishes are still moving** — so the bring-up proceeds to
`utc_start` instead of stalling. It fails closed (does *not* override) if the
DEC service is silent, the array isn't on target, or the moving count is
unknown. This only matters in **live** mode (in shadow nothing is sent
regardless), it still runs the full gate engine, and both the console autopilot
and the h23 supervisor honour it (they read the same policy). Set
`arm_on_dec_ready: false` to require the dashboard's `safe_to_arm` again.

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

#### Handing a running schedule off to h23

Because the armed plan lives in `h23`'s etcd (not in your browser), it survives
you walking away. The handoff is automatic via the single lease:

1. Arm + start the schedule from your laptop (your console holds the lease and
   its autopilot drives the bring-up).
2. Click **Release** in the **Control** tab (or just let the laptop sleep — the
   lease lapses after ~30 s).
3. The standing supervisor on h23 **reclaims the now-free lease on its next
   heartbeat (≤10 s) and continues the same armed plan** from wherever it is.
   It steps back to monitor-only while you hold control, then takes over the
   moment you release — it never fights you for the lease.

**No policy change is needed for this.** `autonomy.run_plan` is on by default,
and the supervisor obeys the *same* `shadow`/`live` + promotion gates as the
console — so to make h23 actually move antennas (not dry-run), it needs
`mode: live` + the relevant actions promoted, exactly as in
[Going live](#going-live-flipping-the-policy). The only requirement is that the
supervisor process is actually running on h23 (`python -m
dsa_operator.monitor.supervisor` or its systemd unit); start it any time — if
your laptop holds the lease when it boots, it starts monitor-only and takes
over automatically once you release.

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

## Seeing what happened (and what failed)

Failures are **never silent**. There are four places to look, from glanceable
to forensic:

1. **The Activity feed** (the strip directly under the status bar, always
   visible). It auto-refreshes every ~7 s and lists the most recent control
   actions and bring-up steps, newest first. Each row shows the time, a
   `live`/`shadow` tag, the action, and the outcome note. **Failures are red**
   and prefixed `✗` (e.g. `utc_start ✗ dashboard POST /control/utc_start
   refused: no captures answering`). Tick **failures only** to filter to just
   the problems. This reads an in-memory ring, so it's instant and always up
   to date.

2. **The bring-up pill** in the status bar shows the autopilot's live stage
   while a plan is armed and you hold the lease: `point`, `warm (waiting)`,
   `arm`, `done`, or a red **`BLOCKED @ <stage> — <reason>`** when the
   sequence stops. This is the fastest way to see *why* an armed plan isn't
   progressing.

3. **Advance bring-up** (Plan tab) steps the sequence by hand and returns the
   exact stage, decision, and blocker for the current step — use it to probe
   interactively.

4. **The durable audit log** — the system of record. Every action is recorded
   three ways: an append-only local JSONL file (under
   `DSA_OPERATOR_AUDIT_ROOT`, default `audit_log/audit-YYYYMMDD.jsonl`), the
   shared etcd audit trail, and — if configured — a Slack summary. Every line
   carries the operator name, the `mode` (`live`/`shadow`), `ok` true/false,
   and a `note`. View recent rows in the **Monitor → Audit** view (or the
   **full log →** link on the Activity strip), or tail the file directly.

> **Why a plan could "complete" with nothing happening — now caught.** A live
> control action is only real if it is **promoted** *and* the dashboard
> actually accepts it. Two cases used to pass silently and now surface as
> explicit rows in the feed:
>
> - **Shadow no-op in live mode** — an action that isn't in your
>   `config/local.yaml` `promote:` list runs as a dry-run even when
>   `mode: live`. It appears with a yellow `shadow` tag and a note like
>   `policy mode=live but 'restart_all' is not promoted; shadow only`.
> - **Dashboard refusal / wrong route** — the executor now checks the
>   dashboard's HTTP status and JSON `ok` flag. A 404 (route not exposed), a
>   5xx, or an app-level refusal (HTTP 200 `{ok:false}`, e.g. `utc_start` with
>   no captures answering) is recorded as a **failure** and **blocks the
>   bring-up** with the real reason, instead of advancing as if it worked.

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
| Armed a plan but nothing moves | mode is shadow (dry-run), you don't hold the lease, or actions aren't promoted | check the `mode` + `control` + `bring-up` status-bar pills and the **Activity feed** (failures are red). Go live, acquire the lease, and promote the bring-up actions. Click **Advance bring-up** (Plan tab) to see the exact stage/blocker. |
| Plan runs on h23 but moves nothing | h23 has no `config/local.yaml` → everything is a shadow no-op | create `config/local.yaml` on h23 with the same `promote:` list as the laptop; the Activity feed shows yellow `shadow` rows when this happens — see [Setting up the laptop and h23](#setting-up-the-laptop-and-h23) |
| Activity feed shows `✗ … refused: no captures answering` | the dashboard declined `utc_start` because the fleet isn't actually capturing | the pipeline isn't started/warm — bring the fleet up first; the bring-up now blocks here instead of silently "completing" |
| "system_state stuck at ready, safe_to_arm=false" | pipeline not started yet, or a few dishes still settling | the bring-up runs `start_fleet` then waits for warm-up; in shadow it never really starts — go live. If the array is healthy but `safe_to_arm` stays false (e.g. a couple of dishes settling), the **dec-ready override** arms anyway once on target with ≤ `max_moving_antennas` moving — see [Arming when safe_to_arm stays false](#arming-when-safe_to_arm-stays-false) |
| Agent won't act at all | e-stop engaged or dashboard lockout | resume the e-stop; check the dsa110-rt authority panel |
| `corr_fast` stalls after ~N blocks | missing fstable → `meridian_fringestop` crash → buffers back up | build + deploy the fstable for the current dec; restart fleet |
| Injections not detected | noise EMA not converged / wrong apply-at | check warm-up convergence and `apply_at_specnum` |
