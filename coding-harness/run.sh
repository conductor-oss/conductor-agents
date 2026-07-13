#!/usr/bin/env bash
# Bootstrap and run the Conductor Coding Harness.
#
#   ./run.sh             set up, register definitions, and run workers
#   ./run.sh setup       install worker dependencies only
#   ./run.sh register    register/update definitions and run the worker gate
#   ./run.sh tui         install and launch the terminal UI
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"

export CONDUCTOR_SERVER_URL="${CONDUCTOR_SERVER_URL:-http://localhost:8080/api}"
WORKER_PY="workers/.venv/bin/python"
TUI_PY="tui/.venv/bin/python"

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: '$1' is required" >&2
    exit 1
  }
}

ensure_server() {
  if conductor workflow list >/dev/null 2>&1; then
    return
  fi
  case "$CONDUCTOR_SERVER_URL" in
    http://localhost:*|http://127.0.0.1:*)
      echo "[coding-harness] starting local Conductor server…"
      conductor server start
      ;;
    *)
      echo "ERROR: Conductor server is unreachable: $CONDUCTOR_SERVER_URL" >&2
      exit 1
      ;;
  esac
  conductor workflow list >/dev/null
}

setup_workers() {
  require python3
  if [ ! -x "$WORKER_PY" ]; then
    echo "[coding-harness] creating worker environment…"
    python3 -m venv workers/.venv
  fi
  workers/.venv/bin/pip install -q -r workers/requirements.txt
}

register() {
  require conductor
  require jq
  ensure_server
  ./workers/register.sh
}

case "${1:-run}" in
  run)
    require conductor
    setup_workers
    register
    echo "[coding-harness] workers polling $CONDUCTOR_SERVER_URL (ctrl-c to stop)"
    exec "$WORKER_PY" workers/main.py
    ;;
  setup)
    setup_workers
    ;;
  register)
    register
    ;;
  tui)
    require python3
    if [ ! -x "$TUI_PY" ]; then
      python3 -m venv tui/.venv
    fi
    tui/.venv/bin/pip install -q -r tui/requirements.txt
    exec "$TUI_PY" -m tui
    ;;
  -h|--help|help)
    sed -n '2,7p' "$ROOT/run.sh"
    ;;
  *)
    echo "usage: ./run.sh [setup|register|tui|help]" >&2
    exit 2
    ;;
esac
