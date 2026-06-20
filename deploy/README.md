# Deploying dsa110-operator (laptop / ops workstation)

Four **user** systemd units (run as the operator, never root):

| Unit | Role |
| --- | --- |
| `dsa110-operator-tunnel.service` | SSH `-L` forwards: h23 etcd → `:12379`, dashboard → `:15778` (auto-reconnects). |
| `dsa110-operator-web.service` | Flask console + chat (local identity, no SSO; depends on the tunnel). |
| `dsa110-operator-supervisor.service` | The single standing executor (autonomy loops) on a laptop/workstation **via the tunnel**. Run **at most one** site-wide. |
| `dsa110-operator-supervisor-h23.service` | The same standing executor, running **directly on h23** (no tunnel). Recommended home for the single executor. |

> These `.service` files are **templates** — they contain `@REPO@` and
> `@PYTHON@` placeholders, **not** a hardcoded `~/dsa110-operator` path or a
> specific interpreter. Install them with `scripts/install_service.sh`, which
> fills in your actual clone location (anywhere) and your python (your active
> conda env by default). Don't `cp` them directly.

## Install

The clone can live **anywhere** and run under **any python** (a conda env is
the common case). `scripts/install_service.sh` resolves both for you.

```bash
# 1. code + deps — clone wherever you like; use a conda env or a venv.
git clone git@github.com:dsa110/dsa110-operator.git
cd dsa110-operator
conda activate myenv            # or: python3.10 -m venv .venv && . .venv/bin/activate
pip install -e '.[etcd,web,agent]'

# 2. SSH alias for h23 with a passwordless key (~/.ssh/config: Host h23 ...)
ssh h23 true        # must succeed non-interactively

# 3. secrets (NOT in git; chmod 600) — optional for read-only/stub use
mkdir -p ~/.config/dsa110-operator
cp .env.example ~/.config/dsa110-operator/operator.env
$EDITOR ~/.config/dsa110-operator/operator.env   # API key, Slack, name, etc.
chmod 600 ~/.config/dsa110-operator/operator.env

# 4. generate + install the units for THIS machine, then start them.
#    With the conda env active, its python is picked automatically.
scripts/install_service.sh laptop --enable
loginctl enable-linger "$USER"   # keep running after logout
```

`install_service.sh laptop` installs the tunnel + web (+ an optional, not
auto-started supervisor) units, substituting your clone path and python. Pick a
specific interpreter with `--python /path/to/python`; omit `--enable` to install
without starting. Open the console at <http://127.0.0.1:8787>. There is no login.

## Try it locally (no API key)

A quick laptop trial — monitoring + Q&A with no Anthropic key (falls back to
the deterministic stub agent). The startup script does the tunnel + console:

```bash
cd dsa110-operator
conda activate myenv && pip install -e '.[etcd,web]'   # or a .venv
scripts/laptop.sh                 # opens tunnel + console; Ctrl-C to stop
```

Open <http://127.0.0.1:8787> (no login) and use the **Monitor** tab + chat. To
exercise the real Claude brain and control, add `ANTHROPIC_API_KEY` to
`~/.config/dsa110-operator/secrets.env`.

> The etcd client sets `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python`
> automatically, so the known etcd3/protobuf import error doesn't bite.

## The standing supervisor on h23 (recommended)

The single standing executor is best run **on h23 itself**. It is headless —
it **exposes no port**, so nothing has to reach it; it just holds the lease and
runs the loops. On h23, etcd and the dashboard are local, so it needs **no SSH
tunnel** — point it straight at them:

The etcd/dashboard endpoints are baked into the unit, so no `operator.env` is
required. Clone anywhere; run under the `dsart` (or any) conda env that already
has `etcd3` / `pyyaml` / `requests` — the installer picks the active env.

```bash
# on h23, as the operator account — clone wherever you like
git clone git@github.com:dsa110/dsa110-operator.git
cd dsa110-operator
conda activate dsart_h23          # already has etcd3 / pyyaml / requests

# generate + install the h23 unit for THIS clone + this python, then start it.
scripts/install_service.sh h23 --enable
loginctl enable-linger "$USER"    # survive logout

# optional: add Slack alerts later
mkdir -p ~/.config/dsa110-operator
echo 'DSA_OPERATOR_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...' \
  >> ~/.config/dsa110-operator/operator.env
chmod 600 ~/.config/dsa110-operator/operator.env
systemctl --user restart dsa110-operator-supervisor-h23
```

The installer fills the unit's `@REPO@`/`@PYTHON@` with your clone path and the
active env's python (override with `--python /path/to/python`). `PYTHONPATH` is
set to `<clone>/src`, so you do **not** need `pip install -e` — only the runtime
deps must be in that python. To run it interactively first (before installing
the service), use the script, which sets the h23 defaults for you:

```bash
scripts/h23_supervisor.sh
```

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
