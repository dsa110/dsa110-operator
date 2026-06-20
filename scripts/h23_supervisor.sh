#!/usr/bin/env bash
#
# Run the standing autonomy supervisor DIRECTLY ON h23 (no SSH tunnel).
#
# This is the recommended home for the single standing executor. It is
# headless (exposes NO port) and reaches etcd + the dsa_monitor dashboard
# locally on h23. It acquires the operator lease as session "supervisor" and
# runs the policy-gated health / recovery / injection / armed-plan loops.
#
# Run AT MOST ONE of these across the whole site.
#
# Usage (on h23):
#   scripts/h23_supervisor.sh
#
# For an always-on service instead, install the systemd unit:
#   deploy/dsa110-operator-supervisor-h23.service  (see deploy/README.md)
#
# Environment (sensible h23 defaults; override as needed):
#   DSA_OPERATOR_ETCD_HOST       (default: etcdv3service.pro.pvt)
#   DSA_OPERATOR_ETCD_PORT       (default: 2379)
#   DSA_OPERATOR_DASHBOARD_PORT  (default: 5778)
#   DSA_OPERATOR_SLACK_WEBHOOK_URL  optional, for health alerts
#   DSA_OPERATOR_ACTOR           audit label for its actions (default: agent)
#   PYTHON                       python interpreter (default: python)
#
# The supervisor is the deterministic, non-LLM loop — NO Anthropic key needed.
set -euo pipefail

PY="${PYTHON:-python}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
[[ -d .venv ]] && source .venv/bin/activate

export DSA_OPERATOR_ETCD_HOST="${DSA_OPERATOR_ETCD_HOST:-etcdv3service.pro.pvt}"
export DSA_OPERATOR_ETCD_PORT="${DSA_OPERATOR_ETCD_PORT:-2379}"
export DSA_OPERATOR_DASHBOARD_PORT="${DSA_OPERATOR_DASHBOARD_PORT:-5778}"

echo "==> supervisor on h23: etcd ${DSA_OPERATOR_ETCD_HOST}:${DSA_OPERATOR_ETCD_PORT}," \
     "dashboard 127.0.0.1:${DSA_OPERATOR_DASHBOARD_PORT}"
echo "    (headless; no port exposed; Ctrl-C to stop)"
exec "$PY" -m dsa_operator.monitor.supervisor "$@"
