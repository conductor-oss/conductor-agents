#!/usr/bin/env bash
# Shared stack bootstrap for ./scan and ./assess — make the harness "just work" from cold.
#
# Sourced by the entry scripts; exposes `sc_ensure_stack <workflow-name>` which idempotently
# brings up everything a run needs, skipping any piece that is already healthy:
#   1. local Conductor server (conductor server start) — waits for /health
#   2. task defs + workflows registered (conductor/register.sh) — checks the workflow exists
#   3. the worker fleet (workers/.venv/bin/python main.py, full module set, backgrounded)
#
# Liveness is checked against the SERVER, not `pgrep`: workers poll ~10x/s, so a live fleet's
# queue `lastPollTime` is always within seconds; a stale lastPollTime (or none) means the fleet
# is down even though the server still remembers the last poll. Set SC_NO_BOOTSTRAP=1 (or pass
# --no-bootstrap, handled by the caller) to manage the stack yourself (CI / custom deploy).

# Repo root: prefer SC_REPO_ROOT exported by the caller (bash $0-derived, reliable); else fall
# back to this file's location. The fallback uses BASH_SOURCE under bash; if sourced from a shell
# without it, the caller-set SC_REPO_ROOT is what keeps resolution correct.
_SC_ROOT="${SC_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
# shellcheck disable=SC1091
source "$_SC_ROOT/conductor/env.sh"
sc_load_conductor_environment "$_SC_ROOT"
_SC_API="${CONDUCTOR_SERVER_URL%/}"
_SC_BASE="${_SC_API%/api}"
_SC_PY="$_SC_ROOT/workers/.venv/bin/python"
_SC_WORKER_MODULES="${WORKER_MODULES:-recon,browser,dast,sast,codenav,api,rag,httptool,codeexec,oob,safety,hc}"
_SC_WORKER_LOG="${SC_WORKER_LOG:-/tmp/sc-workers.log}"
_SC_SERVER_LOG="${SC_SERVER_LOG:-/tmp/sc-conductor-server.log}"
_SC_ACCESS_TOKEN="${CONDUCTOR_AUTH_TOKEN:-}"

_sc_http() {
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-5}" "$1" 2>/dev/null) || {
    echo 000
    return
  }
  echo "$code"
}

_sc_server_up() { conductor workflow list >/dev/null 2>&1; }

_sc_ensure_access_token() {
  [ -z "$_SC_ACCESS_TOKEN" ] || return 0
  [ -n "${CONDUCTOR_AUTH_KEY:-}" ] || return 0
  local body response
  body=$(jq -nc --arg key "$CONDUCTOR_AUTH_KEY" --arg secret "$CONDUCTOR_AUTH_SECRET" \
    '{keyId:$key,keySecret:$secret}')
  response=$(printf '%s' "$body" | curl -sS --max-time 10 \
    -H 'Content-Type: application/json' --data-binary @- "$_SC_API/token") || return 1
  _SC_ACCESS_TOKEN=$(printf '%s' "$response" | jq -r '.token // empty')
  [ -n "$_SC_ACCESS_TOKEN" ]
}

_sc_api_get() {
  local url="$1" output_var="$2" response
  _sc_ensure_access_token || return 1
  if [ -n "$_SC_ACCESS_TOKEN" ]; then
    response=$(curl -sS --max-time 5 -H "X-Authorization: $_SC_ACCESS_TOKEN" "$url") || return 1
  else
    response=$(curl -sS --max-time 5 "$url") || return 1
  fi
  printf -v "$output_var" '%s' "$response"
}

# Workers are LIVE iff a fleet process exists AND a core queue was polled within the last 15s.
# The process check matters: right after `pkill`, the server still reports a <15s-old lastPollTime
# (stale record), so poll-freshness alone would falsely report "live" on a kill+immediate-rerun and
# skip starting a fresh fleet. Requiring a live main.py process closes that race.
_sc_workers_live() {
  pgrep -f "main.py" >/dev/null 2>&1 || return 1
  local pd last now
  _sc_api_get "$_SC_API/tasks/queue/polldata?taskType=http_request" pd || return 1
  last="$(printf '%s' "$pd" | jq -r '[.[].lastPollTime]|max // 0' 2>/dev/null || echo 0)"
  [ "${last:-0}" -gt 0 ] || return 1
  now=$(( $(date +%s) * 1000 ))
  [ $(( now - last )) -lt 15000 ]
}

_sc_registered() { conductor workflow get "$1" >/dev/null 2>&1; }

_sc_ensure_server() {
  if _sc_server_up; then echo "ℹ  conductor server: up"; return 0; fi
  command -v conductor >/dev/null || { echo "ERROR: conductor CLI not found; cannot start the server." >&2; return 1; }
  local http_code server_type
  http_code=$(_sc_http "$_SC_API/metadata/workflow")
  if [ -n "$http_code" ] && [ "$http_code" != "000" ]; then
    if [ "$http_code" = "401" ] || [ "$http_code" = "403" ]; then
      echo "ERROR: Conductor is reachable at $CONDUCTOR_SERVER_URL, but authentication/authorization failed." >&2
      echo "       Check CONDUCTOR_AUTH_KEY and CONDUCTOR_AUTH_SECRET (or CONDUCTOR_AUTH_TOKEN)." >&2
    else
      echo "ERROR: Conductor is reachable at $CONDUCTOR_SERVER_URL (HTTP $http_code)," >&2
      echo "       but 'conductor workflow list' failed. Run that command directly for details." >&2
    fi
    return 1
  fi
  server_type=$(printf '%s' "${CONDUCTOR_SERVER_TYPE:-OSS}" | tr '[:upper:]' '[:lower:]')
  case "$CONDUCTOR_SERVER_URL" in
    http://localhost:*|http://127.0.0.1:*) ;;
    *) echo "ERROR: Conductor server is unreachable: $CONDUCTOR_SERVER_URL" >&2; return 1 ;;
  esac
  if [ -n "${CONDUCTOR_AUTH_KEY:-}${CONDUCTOR_AUTH_SECRET:-}${CONDUCTOR_AUTH_TOKEN:-}" ] || \
     [ "$server_type" = "enterprise" ]; then
    echo "ERROR: Authenticated/Enterprise Conductor is unreachable at $CONDUCTOR_SERVER_URL." >&2
    echo "       Refusing to start a local OSS server in its place." >&2
    return 1
  fi
  echo "ℹ  conductor server: starting (logs: $_SC_SERVER_LOG) …"
  env -u CONDUCTOR_CONFIG_FILE conductor server start >"$_SC_SERVER_LOG" 2>&1 || true
  for _ in $(seq 1 60); do _sc_server_up && { echo "ℹ  conductor server: ready"; return 0; }; sleep 1; done
  echo "ERROR: conductor server did not become healthy (see $_SC_SERVER_LOG)." >&2; return 1
}

_sc_ensure_registered() {
  if _sc_registered "$1"; then echo "ℹ  workflows: registered ($1 present)"; return 0; fi
  echo "ℹ  workflows: registering task defs + workflows …"
  bash "$_SC_ROOT/conductor/register.sh" >/dev/null 2>&1 || { echo "ERROR: register.sh failed." >&2; return 1; }
  _sc_registered "$1" || { echo "ERROR: $1 still not registered after register.sh." >&2; return 1; }
}

_sc_ensure_workers() {
  if _sc_workers_live; then echo "ℹ  workers: live (fleet polling)"; return 0; fi
  [ -x "$_SC_PY" ] || { echo "ERROR: worker venv missing ($_SC_PY). Run 'make venv' first." >&2; return 1; }
  echo "ℹ  workers: starting fleet [$_SC_WORKER_MODULES] (logs: $_SC_WORKER_LOG) …"
  mkdir -p "$_SC_ROOT/state"
  ( cd "$_SC_ROOT/workers" \
    && REPORTS_DIR="${REPORTS_DIR:-$_SC_ROOT/reports}" STATE_DIR="${STATE_DIR:-$_SC_ROOT/state}" \
       WORKER_MODULES="$_SC_WORKER_MODULES" \
       nohup "$_SC_PY" -u main.py >"$_SC_WORKER_LOG" 2>&1 & echo $! >"$_SC_ROOT/state/workers.pid" )
  for _ in $(seq 1 40); do _sc_workers_live && { echo "ℹ  workers: fleet up (pid $(cat "$_SC_ROOT/state/workers.pid" 2>/dev/null))"; return 0; }; sleep 1; done
  echo "ERROR: worker fleet did not start polling (see $_SC_WORKER_LOG)." >&2; return 1
}

# Public entry: ensure server + registration + workers for a given workflow. Idempotent.
sc_ensure_stack() {
  local wf="${1:-deep_assess}"
  [ -n "${SC_NO_BOOTSTRAP:-}" ] && { echo "ℹ  SC_NO_BOOTSTRAP set — skipping stack bootstrap."; return 0; }
  command -v jq >/dev/null || { echo "ERROR: jq required for bootstrap." >&2; return 1; }
  _sc_ensure_server || return 1
  _sc_ensure_registered "$wf" || return 1
  _sc_ensure_workers || return 1
  echo "ℹ  stack ready — launching."
}
