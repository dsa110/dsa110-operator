# Deploying dsa110-operator (laptop / ops workstation)

Three **user** systemd units (run as the operator, never root):

| Unit | Role |
| --- | --- |
| `dsa110-operator-tunnel.service` | SSH `-L` forwards: h23 etcd → `:12379`, dashboard → `:15778`. |
| `dsa110-operator-web.service` | Flask console + Google SSO + chat (depends on the tunnel). |
| `dsa110-operator-supervisor.service` | The single standing executor (autonomy loops, off by default). Run **at most one** site-wide. |

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
$EDITOR ~/.config/dsa110-operator/operator.env   # API key, OAuth, Slack, etc.
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

Open the console at <http://127.0.0.1:8787>.

## Notes

* **Egress:** set `DSA_OPERATOR_ENFORCE_EGRESS=1` in `operator.env` to arm
  the in-process DNS tripwire (defense-in-depth; the host firewall is still
  the primary control — only `config/egress_allowlist.yaml` hosts + SSH to
  h23 + loopback should be reachable).
* **Slack:** set `DSA_OPERATOR_SLACK_WEBHOOK`; test with
  `python -m dsa_operator.audit.slack --test "hello"`.
* **One executor:** the lease guarantees only one session controls at a
  time, but don't run two supervisors — the second will refuse the lease.
* **Lingering:** `loginctl enable-linger $USER` keeps user units running
  after logout.
