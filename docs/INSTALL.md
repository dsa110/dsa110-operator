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

```bash
git clone git@github.com:dsa110/dsa110-operator.git ~/dsa110-operator
cd ~/dsa110-operator
python3.10 -m venv .venv && . .venv/bin/activate
pip install -e '.[etcd,web,agent]'      # add ,dev for the test suite
```

Optional extras: `etcd` (the live etcd client), `web` (the console),
`agent` (the Claude brain), `dev` (pytest). Without `agent` or an API key the
console still runs with a deterministic stub assistant.

> If `etcd3` fails to import with a protobuf error, run commands with
> `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` (an upstream `etcd3`
> packaging quirk).

## Try it in 5 minutes (no OAuth, no API key)

The fastest way to confirm everything is wired:

```bash
# 1. open the tunnel (leave running)
python -m dsa_operator.transport.ssh_tunnel --ssh-host h23 &

# 2. smoke the read-only tools against live h23
python -m dsa_operator.tools.readonly

# 3. run the console with the local dev-login bypass
export DSA_OPERATOR_DEV_LOGIN=1          # FakeAuth — localhost only!
export DSA_OPERATOR_SECRET_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')
python -m dsa_operator.web.app           # http://127.0.0.1:8787
```

`DSA_OPERATOR_DEV_LOGIN` skips Google sign-in for local testing only — never
use it on anything reachable off `localhost`. See [USAGE](USAGE.md) for what
to do once it's open.

## Full configuration

For real use, configure secrets and Google SSO. Put secrets in a
**git-ignored** file — either `./.env` in the repo or
`~/.config/dsa110-operator/secrets.env` (see `.env.example`):

```ini
# The one Anthropic key that funds the agent (your account). Without it the
# console falls back to the deterministic stub. Prefer a workspace key with
# a spend cap.
ANTHROPIC_API_KEY=sk-ant-...

# Google OAuth for sign-in. Authorize this redirect URI in Google Cloud:
#   http://localhost:8787/auth/callback
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...

# Who may sign in (monitoring). A domain allowlist or explicit emails.
DSA_OPERATOR_ALLOWED_DOMAINS=dsa110.org
# DSA_OPERATOR_ALLOWED_EMAILS=alice@x.org,bob@y.org

# Long random string signing the session cookie.
DSA_OPERATOR_SECRET_KEY=...

# Optional: Slack notifications for control/alerts.
# DSA_OPERATOR_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

# Optional: the agent model.
# DSA_OPERATOR_MODEL=claude-sonnet-4-5
```

### Setting up Google OAuth

1. In the Google Cloud console create an **OAuth 2.0 Client ID** (type: Web
   application).
2. Add `http://localhost:8787/auth/callback` as an authorized redirect URI.
3. Copy the client ID/secret into the secrets file.
4. Set `DSA_OPERATOR_ALLOWED_DOMAINS` (or `_ALLOWED_EMAILS`) to the people
   who may sign in. Unlisted accounts are denied and the denial is audited.

### The Anthropic key

One key funds the agent. Monitoring users never receive a key — they sign in
over SSO and the server makes the Anthropic calls on the shared account.
Nothing secret is ever logged or committed.

### Slack (optional)

Set `DSA_OPERATOR_SLACK_WEBHOOK_URL` and verify:

```bash
python -m dsa_operator.audit.slack --test "operator slack test"
```

### Egress lockdown (optional, recommended)

Set `DSA_OPERATOR_ENFORCE_EGRESS=1` to arm an in-process DNS tripwire that
fails closed for any host not in `config/egress_allowlist.yaml` (Anthropic,
Google, Slack) plus loopback. The host firewall is still the primary control.

## Running it as a service

For an always-available console, install the three user systemd units in
`deploy/` (tunnel, web console, and — optionally — the autonomy supervisor).
See [`deploy/README.md`](../deploy/README.md) for the runbook. Secrets come
from a git-ignored `EnvironmentFile`.

## Multiple users

There is **no shared server**. Each person runs their own console on their
own machine (each with their own `h23` SSH access and a copy of the Anthropic
key). The single-executor **lease lives in `h23`'s etcd**, so across every
running console only **one** session can hold control at a time — the others
are monitor-only until it's released. This is how "many watchers, one
controller" works without a central host.

If you want unprompted autonomy (the standing supervisor), run it on **one**
machine that stays on — it holds the lease as session `supervisor`. Don't run
two; the second will not get the lease.

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
