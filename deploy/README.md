# Deploying dsa110-operator (laptop / ops workstation)

Three **user** systemd units (run as the operator, never root):

| Unit | Role |
| --- | --- |
| `dsa110-operator-tunnel.service` | SSH `-L` forwards: h23 etcd → `:12379`, dashboard → `:15778` (auto-reconnects). |
| `dsa110-operator-web.service` | Flask console + chat (local identity, no SSO; depends on the tunnel). |
| `dsa110-operator-supervisor.service` | The single standing executor (autonomy loops) on a laptop/workstation **via the tunnel**. Run **at most one** site-wide. |
| `dsa110-operator-supervisor-h23.service` | The same standing executor, running **directly on h23** (no tunnel). Recommended home for the single executor. |

## Install

```bash
# 1. code + venv
git clone git@github.com:dsa110/dsa110-operator.git ~/dsa110-operator
cd ~/dsa110-operator
python3.10 -m venv .venv && . .venv/bin/activate
pip install -e '.[etcd,web,agent]'

# 2. SSH alias for h23 with a passwordless key (~/.ssh/config: Host h23 ...)
ssh h23 true        # must succeed non-interactively

# 3. secrets (NOT in git; chmod 600)
mkdir -p ~/.config/dsa110-operator
cp .env.example ~/.config/dsa110-operator/operator.env
$EDITOR ~/.config/dsa110-operator/operator.env   # API key, Slack, name, etc.
chmod 600 ~/.config/dsa110-operator/operator.env

# 4. install + start the units
mkdir -p ~/.config/systemd/user
cp deploy/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dsa110-operator-tunnel
systemctl --user enable --now dsa110-operator-web
# start the supervisor ONLY when you want the standing executor:
# systemctl --user enable --now dsa110-operator-supervisor
```

Open the console at <http://127.0.0.1:8787>. There is no login.

## Try it locally (no API key)

A quick laptop trial — monitoring + Q&A with no Anthropic key (falls back to
the deterministic stub agent). The startup script does the tunnel + console:

```bash
cd ~/dsa110-operator && python3.10 -m venv .venv && . .venv/bin/activate
pip install -e '.[etcd,web]'
scripts/laptop.sh                 # opens tunnel + console; Ctrl-C to stop
```

Open <http://127.0.0.1:8787> (no login) and use the **Monitor** tab + chat. To
exercise the real Claude brain and control, add `ANTHROPIC_API_KEY` to
`~/.config/dsa110-operator/secrets.env`.

> If `etcd3` fails to import with a protobuf error, run with
> `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python`.

## The standing supervisor on h23 (recommended)

The single standing executor is best run **on h23 itself**. It is headless —
it **exposes no port**, so nothing has to reach it; it just holds the lease and
runs the loops. On h23, etcd and the dashboard are local, so it needs **no SSH
tunnel** — point it straight at them:

```bash
# on h23, as the operator account
git clone git@github.com:dsa110/dsa110-operator.git ~/dsa110-operator
cd ~/dsa110-operator && python3.10 -m venv .venv && . .venv/bin/activate
pip install -e '.[etcd]'          # no web/agent needed; supervisor is non-LLM

mkdir -p ~/.config/dsa110-operator
cat > ~/.config/dsa110-operator/operator.env <<'EOF'
DSA_OPERATOR_ETCD_HOST=etcdv3service.pro.pvt
DSA_OPERATOR_ETCD_PORT=2379
DSA_OPERATOR_DASHBOARD_PORT=5778
# DSA_OPERATOR_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
EOF
chmod 600 ~/.config/dsa110-operator/operator.env

mkdir -p ~/.config/systemd/user
cp deploy/dsa110-operator-supervisor-h23.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dsa110-operator-supervisor-h23
loginctl enable-linger $USER      # survive logout
```

To run it interactively first (before installing the service), just use the
script — it sets the h23 defaults for you:

```bash
scripts/h23_supervisor.sh
```

The script uses whatever virtualenv/conda env is already active (e.g. the
`dsart` env, which already has `etcd3` / `pyyaml` / `requests`) and adds `src/`
to `PYTHONPATH`, so you do **not** need a separate venv or `pip install -e` just
to try it. For the systemd unit, point its `ExecStart` at an interpreter that
has those deps (a repo `.venv`, or the dsart conda python — see the comments in
`dsa110-operator-supervisor-h23.service`).

Your laptops still each run their own console over the tunnel and contend for
the lease normally; the h23 supervisor is simply the default lease-holder that
drives autonomy + armed plans when no laptop has taken over. Watch it with
`journalctl --user -u dsa110-operator-supervisor-h23 -f`, or via the audit
trail / Slack.

## Notes

* **Egress:** set `DSA_OPERATOR_ENFORCE_EGRESS=1` in `operator.env` to arm
  the in-process DNS tripwire (defense-in-depth; the host firewall is still
  the primary control — only `config/egress_allowlist.yaml` hosts + SSH to
  h23 + loopback should be reachable).
* **Slack:** set `DSA_OPERATOR_SLACK_WEBHOOK_URL`; test with
  `python -m dsa_operator.audit.slack --test "hello"`.
* **Sleep/resume:** the tunnel unit (`Restart=always`) and the web console's
  lease keepalive recover automatically; a lease held across a suspend lapses
  on h23 (control auto-frees) and the UI prompts a re-acquire on wake.
* **One executor:** the lease guarantees only one session controls at a
  time, but don't run two supervisors — the second will refuse the lease.
* **Lingering:** `loginctl enable-linger $USER` keeps user units running
  after logout.
