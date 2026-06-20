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
# read automatically from ./.env or ~/.config/dsa110-operator/secrets.env.
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

echo "==> opening self-healing SSH tunnel to $SSH_HOST ..."
"$PY" -m dsa_operator.transport.ssh_tunnel --ssh-host "$SSH_HOST" \
      --etcd-port "$ETCD_PORT" &
TUN_PID=$!
cleanup() { echo; echo "==> stopping tunnel"; kill "$TUN_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "==> waiting for the tunnel (etcd -> 127.0.0.1:${ETCD_PORT}) ..."
for _ in $(seq 1 60); do
  if (exec 3<>"/dev/tcp/127.0.0.1/${ETCD_PORT}") 2>/dev/null; then
    exec 3>&- 3<&- ; break
  fi
  sleep 0.5
done

echo "==> starting console at http://127.0.0.1:${PORT}   (no login)"
DSA_OPERATOR_PORT="$PORT" "$PY" -m dsa_operator.web.app
