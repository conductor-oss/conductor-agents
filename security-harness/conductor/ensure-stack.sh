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
_SC_BASE="${CONDUCTOR_SERVER_URL:-http://localhost:8080/api}"; _SC_BASE="${_SC_BASE%/api}"
_SC_PY="$_SC_ROOT/workers/.venv/bin/python"
_SC_WORKER_MODULES="${WORKER_MODULES:-recon,browser,dast,sast,codenav,api,rag,httptool,codeexec,oob,safety,hc}"
_SC_WORKER_LOG="${SC_WORKER_LOG:-/tmp/sc-workers.log}"
_SC_SERVER_LOG="${SC_SERVER_LOG:-/tmp/sc-conductor-server.log}"

_sc_http() { curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-5}" "$1" 2>/dev/null || echo 000; }

_sc_server_up() { [ "$(_sc_http "$_SC_BASE/health")" = "200" ]; }

# Workers are LIVE iff a fleet process exists AND a core queue was polled within the last 15s.
# The process check matters: right after `pkill`, the server still reports a <15s-old lastPollTime
# (stale record), so poll-freshness alone would falsely report "live" on a kill+immediate-rerun and
# skip starting a fresh fleet. Requiring a live main.py process closes that race.
_sc_workers_live() {
  pgrep -f "main.py" >/dev/null 2>&1 || return 1
  local pd last now
  pd="$(curl -s --max-time 5 "$_SC_BASE/api/tasks/queue/polldata?taskType=http_request" 2>/dev/null)"
  last="$(printf '%s' "$pd" | jq -r '[.[].lastPollTime]|max // 0' 2>/dev/null || echo 0)"
  [ "${last:-0}" -gt 0 ] || return 1
  now=$(( $(date +%s) * 1000 ))
  [ $(( now - last )) -lt 15000 ]
}

_sc_registered() { [ "$(_sc_http "$_SC_BASE/api/metadata/workflow/$1")" = "200" ]; }

_sc_ensure_server() {
  if _sc_server_up; then echo "ℹ  conductor server: up"; return 0; fi
  command -v conductor >/dev/null || { echo "ERROR: conductor CLI not found; cannot start the server." >&2; return 1; }
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
