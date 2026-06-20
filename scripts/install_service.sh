#!/usr/bin/env bash
#
# Install dsa110-operator systemd USER units with paths + python resolved for
# THIS machine. Nothing is hardcoded: the clone can live anywhere and the
# services run under whatever python you point at (a conda env by default).
#
# Usage:
#   scripts/install_service.sh <laptop|h23> [--python /path/to/python] [--enable]
#
#   laptop   install tunnel + web (+ optional supervisor) units for the console
#   h23      install the standing supervisor that runs ON h23 (no tunnel)
#
# Options:
#   --python PATH   interpreter to run the services with. Default order:
#                   active conda env -> active venv -> repo .venv -> python3.
#   --enable        also `systemctl --user enable --now` the core units.
#
# After installing, keep services alive across logout with:
#   loginctl enable-linger "$USER"
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="${REPO_ROOT}/deploy"
UNIT_DIR="${HOME}/.config/systemd/user"

usage() { sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

MODE="${1:-}"; [[ $# -gt 0 ]] && shift || true
PYTHON=""
ENABLE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) PYTHON="${2:?--python needs a path}"; shift 2;;
    --enable) ENABLE=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown arg: $1" >&2; usage; exit 1;;
  esac
done

if [[ "$MODE" != "laptop" && "$MODE" != "h23" ]]; then
  echo "ERROR: first argument must be 'laptop' or 'h23'." >&2
  usage; exit 1
fi

# Resolve the python interpreter: explicit > active conda > active venv >
# repo .venv > python3 on PATH. Most users run under a conda env, so an active
# CONDA_PREFIX wins automatically.
if [[ -z "$PYTHON" ]]; then
  if [[ -n "${CONDA_PREFIX:-}" && -x "${CONDA_PREFIX}/bin/python" ]]; then
    PYTHON="${CONDA_PREFIX}/bin/python"
  elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    PYTHON="${VIRTUAL_ENV}/bin/python"
  elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON="${REPO_ROOT}/.venv/bin/python"
  else
    PYTHON="$(command -v python3 || true)"
  fi
fi
[[ -n "$PYTHON" && -x "$PYTHON" ]] || {
  echo "ERROR: no usable python found (got '${PYTHON:-}'). Pass --python PATH." >&2
  exit 1
}

echo "==> repo:   $REPO_ROOT"
echo "==> python: $PYTHON"

# Non-fatal dependency check so failures are obvious before systemd retries.
# Mirror the runtime protobuf workaround so etcd3 imports cleanly here too.
if ! PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python "$PYTHON" - <<'PY' >/dev/null 2>&1
import etcd3, yaml, requests  # noqa: F401
PY
then
  echo "WARNING: '$PYTHON' is missing runtime deps (etcd3 / pyyaml / requests)." >&2
  echo "         Install into that env, e.g.:" >&2
  echo "           $PYTHON -m pip install etcd3 pyyaml requests" >&2
  echo "         (the laptop console's real LLM agent also needs 'anthropic')." >&2
fi

mkdir -p "$UNIT_DIR"

install_unit() {  # <template-filename>
  local name="$1"
  sed -e "s|@REPO@|${REPO_ROOT}|g" -e "s|@PYTHON@|${PYTHON}|g" \
      "${DEPLOY}/${name}" > "${UNIT_DIR}/${name}"
  echo "    -> ${UNIT_DIR}/${name}"
}

core=()
if [[ "$MODE" == "laptop" ]]; then
  install_unit dsa110-operator-tunnel.service
  install_unit dsa110-operator-web.service
  install_unit dsa110-operator-supervisor.service   # optional; not auto-enabled
  core=(dsa110-operator-tunnel dsa110-operator-web)
else
  install_unit dsa110-operator-supervisor-h23.service
  core=(dsa110-operator-supervisor-h23)
fi

systemctl --user daemon-reload
echo "==> systemctl --user daemon-reload done"

if [[ "$ENABLE" == "1" ]]; then
  systemctl --user enable --now "${core[@]}"
  echo "==> enabled + started: ${core[*]}"
  echo "    keep running after logout:  loginctl enable-linger \"$USER\""
else
  echo
  echo "Next:"
  echo "  systemctl --user enable --now ${core[*]}"
  echo "  loginctl enable-linger \"$USER\"   # keep running after logout"
  if [[ "$MODE" == "laptop" ]]; then
    echo "  # optional autonomy executor (only if you want unprompted action):"
    echo "  #   systemctl --user enable --now dsa110-operator-supervisor"
  fi
fi
