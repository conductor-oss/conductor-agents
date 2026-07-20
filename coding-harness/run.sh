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

# Load operator config from .env if present (see .env.example).
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

export CONDUCTOR_SERVER_URL="${CONDUCTOR_SERVER_URL:-http://localhost:8080/api}"
WORKER_PY="workers/.venv/bin/python"
TUI_PY="tui/.venv/bin/python"

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: '$1' is required" >&2
    exit 1
  }
}

# Harness Python version (matches .python-version / pyproject requires-python).
PY_VERSION="3.13"

# Create <dir>/.venv with uv and install <requirements> into it. uv provisions
# Python $PY_VERSION itself (downloading it if the host lacks it), so we never
# fall back to the macOS system python3 (3.9), which has no claude-agent-sdk
# wheels. A stale venv on an older Python is rebuilt (uv venv won't reuse it).
ensure_venv() { # ensure_venv <dir> <requirements-file>
  require uv
  local dir="$1" reqs="$2" vpy="$1/.venv/bin/python"
  if [ ! -x "$vpy" ] || ! "$vpy" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 13) else 1)' 2>/dev/null; then
    echo "[coding-harness] creating $dir environment (uv, Python $PY_VERSION)…"
    uv venv --clear --python "$PY_VERSION" "$dir/.venv"
  fi
  # --prerelease=explicit: openai-codex pins a pre-release cli-bin dep; pip takes
  # it implicitly. `explicit` allows a pre-release only for a package pinned to
  # one (not globally — `allow` would also drag in e.g. an httpx 1.0 dev build).
  uv pip install -q --prerelease=explicit --python "$vpy" -r "$reqs"
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
  ensure_venv workers workers/requirements.txt
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
    ensure_venv tui tui/requirements.txt
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
