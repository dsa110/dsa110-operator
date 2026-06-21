#!/usr/bin/env bash
#
# Start the dsa110-operator console on YOUR laptop.
#
# What it does, in order:
#   1. checks you can `ssh h23` non-interactively,
#   2. opens the self-healing SSH tunnel to h23 (etcd + dashboard) in the
#      background (it reconnects on its own across wifi drops / laptop sleep),
#   3. waits for the tunnel, then runs the web console at
#      http://127.0.0.1:8787  (there is NO login — you are the operator),
#   4. tears the tunnel down when you quit (Ctrl-C).
#
# Usage:
#   scripts/laptop.sh
#
# Optional environment overrides:
#   DSA_OPERATOR_SSH_HOST   ssh alias for h23           (default: h23)
#   DSA_OPERATOR_PORT       console port                (default: 8787)
#   DSA_OPERATOR_ETCD_PORT  local forwarded etcd port   (default: 12379)
#   DSA_OPERATOR_USER       your name in the audit/lease (default: OS user)
#   PYTHON                  python interpreter          (default: python)
#
# Secrets (ANTHROPIC_API_KEY for the real agent, optional Slack webhook) are
# read automatically from ./.env, ./scripts/.env, or
# ~/.config/dsa110-operator/secrets.env.
set -euo pipefail

SSH_HOST="${DSA_OPERATOR_SSH_HOST:-h23}"
PORT="${DSA_OPERATOR_PORT:-8787}"
ETCD_PORT="${DSA_OPERATOR_ETCD_PORT:-12379}"
PY="${PYTHON:-python}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# Use an already-active virtualenv / conda env if there is one; only fall back
# to a repo-local .venv when nothing is active. Either way, make the src-layout
# package importable without requiring `pip install -e`.
# shellcheck disable=SC1091
if [[ -z "${VIRTUAL_ENV:-}" && -z "${CONDA_PREFIX:-}" && -d .venv ]]; then
  source .venv/bin/activate
fi
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

# Persist a session-cookie secret so browser sessions survive console restarts.
if [[ -z "${DSA_OPERATOR_SECRET_KEY:-}" ]]; then
  secret_file="${HOME}/.config/dsa110-operator/secret_key"
  if [[ ! -f "$secret_file" ]]; then
    mkdir -p "$(dirname "$secret_file")"
    "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$secret_file"
    chmod 600 "$secret_file"
  fi
  export DSA_OPERATOR_SECRET_KEY="$(cat "$secret_file")"
fi

echo "==> checking 'ssh $SSH_HOST' ..."
if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$SSH_HOST" true 2>/dev/null; then
  echo "ERROR: 'ssh $SSH_HOST' did not succeed non-interactively." >&2
  echo "       Add a passwordless key and a 'Host $SSH_HOST' entry to ~/.ssh/config." >&2
  exit 1
fi

# A leftover tunnel from a previous run will hold ${ETCD_PORT}, so a fresh ssh
# can't bind it and dies with rc=255. Catch that early with a clear message
# rather than spinning in the reconnect loop.
if (exec 3<>"/dev/tcp/127.0.0.1/${ETCD_PORT}") 2>/dev/null; then
  exec 3>&- 3<&-
  echo "ERROR: 127.0.0.1:${ETCD_PORT} is already in use — a tunnel is probably" >&2
  echo "       already running. Stop it and retry, e.g.:" >&2
  echo "         pkill -f dsa_operator.transport.ssh_tunnel" >&2
  echo "       (or inspect it:  lsof -nP -i :${ETCD_PORT} )" >&2
  exit 1
fi

echo "==> opening self-healing SSH tunnel to $SSH_HOST ..."
"$PY" -m dsa_operator.transport.ssh_tunnel --ssh-host "$SSH_HOST" \
      --etcd-port "$ETCD_PORT" &
TUN_PID=$!
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/${ETCD_PORT}") 2>/dev/null; }
cleanup() {
  echo; echo "==> stopping tunnel"
  kill "$TUN_PID" 2>/dev/null || true
  wait "$TUN_PID" 2>/dev/null || true   # reap python + its ssh child (no orphan)
  # Belt and suspenders: if the ssh child was somehow orphaned and still holds
  # the forwarded etcd port, kill exactly that forward so the next run isn't
  # blocked by "127.0.0.1:${ETCD_PORT} is already in use". The match is the
  # local-forward spec, so it only ever hits this tunnel's ssh.
  for _ in 1 2 3; do
    port_in_use || break
    pkill -f -- "-L 127.0.0.1:${ETCD_PORT}:" 2>/dev/null || true
    sleep 0.3
  done
  stty sane 2>/dev/null || true         # restore the terminal after Ctrl-C
}
trap cleanup EXIT INT TERM

echo "==> waiting for the tunnel (etcd -> 127.0.0.1:${ETCD_PORT}) ..."
ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "$TUN_PID" 2>/dev/null; then
    echo "ERROR: the tunnel process exited — see the 'ssh: ...' lines above for" >&2
    echo "       the reason (auth, host key, or a port already in use)." >&2
    exit 1
  fi
  if (exec 3<>"/dev/tcp/127.0.0.1/${ETCD_PORT}") 2>/dev/null; then
    exec 3>&- 3<&- ; ready=1; break
  fi
  sleep 0.5
done
if [[ "$ready" != "1" ]]; then
  echo "ERROR: tunnel did not come up within 30s. Check the log above; try a" >&2
  echo "       manual run:  $PY -m dsa_operator.transport.ssh_tunnel --ssh-host $SSH_HOST" >&2
  exit 1
fi

echo "==> starting console at http://127.0.0.1:${PORT}   (no login)"
DSA_OPERATOR_PORT="$PORT" "$PY" -m dsa_operator.web.app
