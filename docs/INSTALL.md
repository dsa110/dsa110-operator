# Installing dsa110-operator

The operator runs **on your own machine** (a laptop or workstation) and
reaches the observatory **only over SSH to `h23`**. It is never installed on
`h23` itself. Through that one SSH hop it forwards two local ports — to
`h23`'s etcd and to the `dsa_monitor` dashboard — and does everything else
through them.

```
your machine ──ssh──> h23 ──┬── etcd (etcdv3service.pro.pvt:2379)   [forwarded to localhost:12379]
                            ├── dsa_monitor dashboard (:5778)        [forwarded to localhost:15778]
                            └── data products on /dataz             [over the same ssh]
```

## Prerequisites

1. **Python 3.10+**.
2. **Non-interactive SSH to `h23`.** You must be able to run `ssh h23 true`
   with no password prompt. Add an entry to `~/.ssh/config`:

   ```
   Host h23
       HostName <h23 address or jump path your site uses>
       User <you>
       IdentityFile ~/.ssh/id_ed25519
   ```

   Everyone who will use the console needs their own `h23` SSH access — that
   is the only thing that grants reach to the observatory.

## Install

Clone it **anywhere** and install into **any** environment — a conda env is the
common case (a venv works too). Nothing assumes `~/dsa110-operator`.

```bash
git clone git@github.com:dsa110/dsa110-operator.git
cd dsa110-operator
conda activate myenv                    # or: python3.10 -m venv .venv && . .venv/bin/activate
pip install -e '.[etcd,web,agent]'      # add ,dev for the test suite
```

Optional extras: `etcd` (the live etcd client), `web` (the console),
`agent` (the Claude brain), `dev` (pytest). Without `agent` or an API key the
console still runs with a deterministic stub assistant.

> The etcd client sets `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` itself,
> so the upstream `etcd3`/protobuf import error is handled automatically.

## Easiest start: the startup scripts

Two scripts do everything for you:

```bash
scripts/laptop.sh        # laptop: opens the tunnel + runs the console (no login)
scripts/h23_supervisor.sh   # on h23: runs the standing autonomy supervisor
```

`scripts/laptop.sh` checks your `ssh h23` works, opens the self-healing tunnel,
waits for it, mints+persists a session secret, and starts the console at
<http://127.0.0.1:8787>; Ctrl-C tears the tunnel back down. Both scripts use
whatever conda/venv is active (and add `src/` to `PYTHONPATH`, so they work even
without `pip install -e`), and accept env overrides (see the comments at the top
of each).

## Try it manually (no API key needed)

If you'd rather run the steps yourself:

```bash
# 1. open the tunnel (leave running; it self-heals across laptop sleeps)
python -m dsa_operator.transport.ssh_tunnel --ssh-host h23 &

# 2. smoke the read-only tools against live h23
python -m dsa_operator.tools.readonly

# 3. run the console
export DSA_OPERATOR_SECRET_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')
python -m dsa_operator.web.app           # http://127.0.0.1:8787
```

There is **no login** — the console runs on your laptop, bound to loopback, so
whoever is at the machine is the operator. Without an Anthropic key the console
runs the deterministic stub assistant (monitoring + Q&A still work). See
[USAGE](USAGE.md) for what to do once it's open.

## Full configuration

Put secrets in a **git-ignored** file — either `./.env` in the repo or
`~/.config/dsa110-operator/secrets.env` (see `.env.example`):

```ini
# The Anthropic key that funds the agent (your account). Without it the
# console falls back to the deterministic stub. Prefer a workspace key with
# a spend cap.
ANTHROPIC_API_KEY=sk-ant-...

# Your operator name — used only as the audit/lease label so others can see
# who holds control. Defaults to your OS username if unset.
# DSA_OPERATOR_USER=vikram

# Long random string signing the session cookie (optional on one laptop).
DSA_OPERATOR_SECRET_KEY=...

# Optional: Slack notifications for control/alerts.
# DSA_OPERATOR_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

# Optional: the agent model.
# DSA_OPERATOR_MODEL=claude-sonnet-4-5
```

### Identity (no SSO)

The console is local to your laptop and reached only through the SSH tunnel, so
there is nothing to authenticate against — a login screen would add friction
with no security benefit. Identity is just a label for the audit trail and the
executor lease: it comes from `DSA_OPERATOR_USER`, else your OS username. When
several people each run their own laptop instance, the single-executor lease on
h23 still guarantees only one can control at a time.

### The Anthropic key

The key funds the agent and lives only on your laptop. Without it the console
falls back to the deterministic stub. Nothing secret is ever logged or
committed.

### Slack (optional)

Set `DSA_OPERATOR_SLACK_WEBHOOK_URL` and verify:

```bash
python -m dsa_operator.audit.slack --test "operator slack test"
```

### Egress lockdown (optional, recommended)

Set `DSA_OPERATOR_ENFORCE_EGRESS=1` to arm an in-process DNS tripwire that
fails closed for any host not in `config/egress_allowlist.yaml` (Anthropic and
Slack) plus loopback. The host firewall is still the primary control.

## Running it as a service

For an always-available console, generate + install the user systemd units with
the installer — it resolves your clone path and python (your active conda env by
default), so nothing is hardcoded:

```bash
scripts/install_service.sh laptop --enable    # tunnel + web (+ optional supervisor)
scripts/install_service.sh h23 --enable       # the standing supervisor, on h23
loginctl enable-linger "$USER"                # keep them running after logout
```

Use `--python /path/to/python` to pin a specific interpreter, or omit `--enable`
to install without starting. The `.service` files in `deploy/` are templates
(`@REPO@`/`@PYTHON@` placeholders) that the installer fills in — don't `cp` them
directly. See [`deploy/README.md`](../deploy/README.md) for the full runbook.
Secrets come from an optional git-ignored `EnvironmentFile`.

## Multiple users

There is **no shared server and no login**. Each person runs their own console
on their own machine (each with their own `h23` SSH access and a copy of the
Anthropic key), identified by their `DSA_OPERATOR_USER` / OS name. The
single-executor **lease lives in `h23`'s etcd**, so across every running console
only **one** session can hold control at a time — the others are monitor-only
until it's released. This is how "many watchers, one controller" works without a
central host.

If you want unprompted autonomy (the standing supervisor), run it on **one**
machine that stays on — it holds the lease as session `supervisor` and
re-acquires it automatically if that machine sleeps and wakes. Don't run two;
the second will not get the lease. **The recommended home is h23 itself**: the
supervisor is headless (exposes no port), and on h23 etcd + the dashboard are
local so it needs no tunnel — set `DSA_OPERATOR_ETCD_HOST=etcdv3service.pro.pvt`,
`DSA_OPERATOR_ETCD_PORT=2379`, `DSA_OPERATOR_DASHBOARD_PORT=5778` and run the
`dsa110-operator-supervisor-h23` unit. See [`deploy/README.md`](../deploy/README.md).

## Surviving laptop sleep / disconnects

Closing the lid (or losing wifi) is expected; the pieces recover on their own:

- **Tunnel:** `ServerAliveInterval` makes `ssh` exit within ~45 s of a suspend,
  and the tunnel command supervises itself — it reconnects with backoff on
  wake. (Under systemd, `Restart=always` does the same.) Pass `--no-retry` to
  opt out.
- **Control lease:** the lease has a short TTL and is refreshed in the
  background only while your console is awake. If the laptop sleeps the lease
  **lapses on h23 within ~30 s — control auto-frees**, so a closed laptop can
  never leave the array stuck "owned". On wake the console detects the lapse and
  shows a banner prompting you to re-acquire (someone may have taken over while
  you were gone).
- **A running observation keeps running.** The pipeline executes autonomously on
  the fleet, and the **hard observation-time cap is enforced by the `dsart_rt`
  watchdog on h23**, not by your laptop — so sleeping your laptop neither stops
  a good observation nor risks an endless one.
- **The console UI** re-polls the lease, authority, and observation status
  immediately on focus/visibility/online events after a wake.

## The dsa110-rt side (human authority)

The human override controls — lock agents out, pin the executor, cap the
observation time — live on the **dsa110-rt dashboard**, not in this app.
Those changes shipped to `dsa110-rt` `main` (dashboard "Operator Agent
Authority" panel + a `dsart_rt` observation-time watchdog). No further
dsa110-rt setup is required to run this console.

## Verify

```bash
pip install -e '.[etcd,web,agent,dev]'
pytest -q                 # the full suite runs offline against fakes
python scripts/shakeout.py   # read-only end-to-end check against live h23
```
