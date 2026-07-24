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

# Load operator defaults while preserving explicit process environment values.
# shellcheck disable=SC1091
. "$ROOT/scripts/conductor_env.sh"
load_harness_environment "$ROOT/.env"
WORKER_PY="workers/.venv/bin/python"
TUI_PY="tui/.venv/bin/python"
# uv provisions PY_VERSION itself, so venvs never fall back to the system
# python3 (which lacks claude-agent-sdk wheels on older interpreters).
UV=uv
PY_VERSION=3.13

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: '$1' is required" >&2
    exit 1
  }
}

ensure_server() {
  local probe_output http_code has_auth=false server_type
  probe_output=$(mktemp)
  if conductor workflow list >"$probe_output" 2>&1; then
    rm -f "$probe_output"
    return
  fi

  # A failed authenticated operation does not mean the server is absent. Probe transport
  # reachability separately: any HTTP response (including 401/403/5xx) proves a server exists.
  http_code=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 3 --max-time 5 \
    "${CONDUCTOR_SERVER_URL%/}/metadata/workflow" 2>/dev/null) || http_code=""
  if [ -n "$http_code" ] && [ "$http_code" != "000" ]; then
    if [ "$http_code" = "401" ] || [ "$http_code" = "403" ] || \
       grep -Eiq '(^|[^0-9])(401|403)([^0-9]|$)|unauthorized|forbidden' "$probe_output"; then
      echo "ERROR: Conductor server is reachable at $CONDUCTOR_SERVER_URL, but authentication/authorization failed." >&2
      echo "       Check CONDUCTOR_AUTH_KEY and CONDUCTOR_AUTH_SECRET (or CONDUCTOR_AUTH_TOKEN)." >&2
    else
      echo "ERROR: Conductor server is reachable at $CONDUCTOR_SERVER_URL (HTTP $http_code)," >&2
      echo "       but 'conductor workflow list' failed. Run that command directly for details." >&2
    fi
    rm -f "$probe_output"
    exit 1
  fi
  rm -f "$probe_output"

  if [ -n "${CONDUCTOR_AUTH_KEY:-}" ] || [ -n "${CONDUCTOR_AUTH_SECRET:-}" ] || \
     [ -n "${CONDUCTOR_AUTH_TOKEN:-}" ]; then
    has_auth=true
  fi
  server_type=$(printf '%s' "${CONDUCTOR_SERVER_TYPE:-OSS}" | tr '[:upper:]' '[:lower:]')
  if [ "$has_auth" = true ] || [ "$server_type" = "enterprise" ]; then
    echo "ERROR: Configured Enterprise/authenticated Conductor server is unreachable: $CONDUCTOR_SERVER_URL" >&2
    echo "       Refusing to start a local OSS server in its place." >&2
    exit 1
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
  require uv
  require node
  require npm
  if [ ! -x "$WORKER_PY" ]; then
    echo "[coding-harness] creating worker environment…"
    "$UV" venv --python "$PY_VERSION" workers/.venv
  fi
  "$UV" pip install -q --prerelease=explicit --python "$WORKER_PY" -r workers/requirements.txt
  npm install --silent --no-audit --no-fund --prefix workers/openspec
}

register() {
  require conductor
  require curl
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
    require uv
    setup_workers
    # Keep the server-side contracts in lockstep with the TUI code. This also runs the
    # SIMPLE-task worker gate, so a newly added workflow cannot silently hang after launch.
    register
    if [ ! -x "$TUI_PY" ]; then
      "$UV" venv --python "$PY_VERSION" tui/.venv
    fi
    "$UV" pip install -q --prerelease=explicit --python "$TUI_PY" -r tui/requirements.txt
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
