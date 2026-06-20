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
- **Preview next** — what the runner *would* do now (no move).
- **Tick** — run one step: if the active dec differs from the commanded dec,
  it issues `point_array` **through the gate engine** (so during commissioning
  each plan-driven move still needs approval).
- **Clear** — remove the active plan.

You can also just ask the assistant: "set up an observing plan for 3C48 and
3C147" or "what's observable at dec 40 right now?".

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
