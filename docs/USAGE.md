# Using dsa110-operator

Once it's [installed and running](INSTALL.md), open the console at
<http://127.0.0.1:8787> and sign in with Google (or the dev bypass for local
testing). This page covers day-to-day use.

For the precise list of what the agent may and may not do, see
[CAPABILITIES](CAPABILITIES.md).

## The console at a glance

The window has four tabs. A badge in the header shows whether you are just
**monitoring** or hold the **executor** role.

| Tab | What it's for |
| --- | --- |
| **Monitor** | Live state (fleet, pointing, sky, RFI, injections, candidates, audit) and the assistant chat. |
| **Control** | The executor lease, human-authority/e-stop, proposing control actions, and granting pending approvals. |
| **Plan** | View / set / preview / tick the observing plan. |
| **Autonomy** | The standing supervisor's status and a manual tick. |

## Monitoring and asking questions (anyone)

Any signed-in user can use the **Monitor** tab and chat — no lease required.
Ask things like:

- "Are all nodes up?"
- "What's the array pointed at right now?"
- "Did the last injection get detected?"
- "Summarise the recent audit log."

The assistant answers by calling the read-only tools; every call is logged
under your Google identity. Nothing you do here changes observatory state.

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
are bound to your SSO identity and expire after 5 minutes. `set_policy` needs
**two** different approvers.

### Shadow vs live

By default the system is in **shadow** mode: every control action renders the
exact writes/calls it *would* make and logs them, but **nothing is sent**.
This is the safe way to exercise the whole surface. Real execution requires
both `mode: live` in `config/policy.yaml` and that the specific action has
been graduated (below).

### Graduating actions to live

Promote actions one at a time, after you've watched them behave correctly in
shadow:

1. Copy `config/local.yaml.example` to `config/local.yaml`.
2. Add the validated action under `promote:` (the file documents a sensible
   staged ladder — reversible/low-risk first, fleet-level last).
3. When you're ready for real execution, set `mode: live` in
   `config/policy.yaml`.

Promotion moves an action from its conservative *commissioning* gate to its
*steady-state* gate and is itself audited. `update_fleet_code` and
`set_policy` can never be graduated — they always need a human.

## Observing plans

DSA-110 is a meridian-transit instrument, so a plan is a timed schedule of
**declinations** (a source is observable around its transit, when LST = RA).
In the **Plan** tab (executor only to change):

- **Set plan** — provide transit-centred `sources`
  (`{label, ra_deg, dec_deg, window_min}`) or explicit `segments`
  (`{t_start, t_end, dec_deg, label}`). Validated against the pointing
  envelope.
- **Preview** — the per-segment bring-up the sequencer *would* run (no move).
- **Arm / Disarm** — a staged plan does nothing until armed; arming is the
  commit after you've confirmed the schedule. The autonomy supervisor (or
  **Tick**) then drives the bring-up for the active segment through the gate
  engine.
- **Clear** — remove the active plan.

Every plan-driven action still flows the full gauntlet (lease, e-stop,
dashboard lockout, gate, shadow/live) — arming changes *when* it runs, never
*what is allowed*.

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
(non-LLM) loop that can monitor health, auto-recover known failures, run
periodic injection health-checks, and tick the observing plan.

- Every loop is **off by default**; enable them under `autonomy:` in
  `config/policy.yaml`.
- Monitoring is read-only. The mutating loops act only when their flag is on
  **and** the supervisor holds the lease **and** agents aren't locked out
  **and** the e-stop is clear — and even then each action runs the full gate
  engine.
- Run the standing executor on one always-on machine:
  `python -m dsa_operator.monitor.supervisor` (or the systemd unit). It holds
  the lease as session `supervisor`.

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
trail, and — if configured — a Slack summary. Every line carries the Google
identity that initiated it.

## Troubleshooting

| Symptom | Likely cause | First action |
| --- | --- | --- |
| Console won't load / read tools error | tunnel not open | start `python -m dsa_operator.transport.ssh_tunnel --ssh-host h23` |
| `etcd3` import error (protobuf) | upstream packaging quirk | prefix commands with `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` |
| Sign-in fails | OAuth redirect not authorized / account not allow-listed | check the redirect URI in Google Cloud and `DSA_OPERATOR_ALLOWED_DOMAINS/_EMAILS` |
| Chat says "denied" for control | you don't hold the lease, or agents are locked out | acquire the lease (Control tab); check dashboard authority |
| Action returns "shadow" | not promoted / mode is shadow | this is expected until you graduate it (see above) |
| Agent won't act at all | e-stop engaged or dashboard lockout | resume the e-stop; check the dsa110-rt authority panel |
| `corr_fast` stalls after ~N blocks | missing fstable → `meridian_fringestop` crash → buffers back up | build + deploy the fstable for the current dec; restart fleet |
| Injections not detected | noise EMA not converged / wrong apply-at | check warm-up convergence and `apply_at_specnum` |
