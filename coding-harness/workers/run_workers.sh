#!/bin/bash
# Supervised worker poller: restarts on crash with backoff so a transient
# failure (network blip, OOM) doesn't take the harness offline.
#
#   CONDUCTOR_SERVER_URL=http://localhost:8080/api ./run_workers.sh
#
# Split workers across hosts by setting WORKER_MODULES (comma-separated;
# default coding_agent,gitops), e.g.
#   WORKER_MODULES=coding_agent ./run_workers.sh   # heavy: LLM coding sessions
#   WORKER_MODULES=gitops ./run_workers.sh         # light: git + GitHub tasks
set -u
WORKERS_DIR=$(cd "$(dirname "$0")" && pwd)
HARNESS_ROOT=$(cd "$WORKERS_DIR/.." && pwd)
# ``.env`` supplies defaults, but an operator splitting worker roles across
# hosts must be able to override it at the invocation boundary.
WORKER_MODULES_WAS_SET="${WORKER_MODULES+x}"
REQUESTED_WORKER_MODULES="${WORKER_MODULES-}"
# shellcheck disable=SC1091
. "$HARNESS_ROOT/scripts/conductor_env.sh"
load_harness_environment "$HARNESS_ROOT/.env" || exit $?
if [[ "$WORKER_MODULES_WAS_SET" == "x" ]]; then
  export WORKER_MODULES="$REQUESTED_WORKER_MODULES"
fi
cd "$WORKERS_DIR"
PY=.venv/bin/python
DELAY=5
export PATH="$WORKERS_DIR/.venv/bin:$PATH"
# The service normalizes an unset response timeout to its server default. Keep
# every in-flight task alive with lease extensions so a long, healthy agent or
# verifier is never failed solely for taking time.
export CONDUCTOR_WORKER_ALL_LEASE_EXTEND_ENABLED=true
echo "[run_workers] CONDUCTOR_SERVER_URL=$CONDUCTOR_SERVER_URL modules=${WORKER_MODULES:-<all>}"

# GitHub-backed task modules need a credential, but the supervisor must remain
# available for local-only work when it is absent.  Probe at startup so a
# launchd/TUI environment regression is explicit in the worker log instead of
# first surfacing as an opaque failure in a PR task.
case ",${WORKER_MODULES:-coding_agent,gitops,campaign,openspec,automation,model_policy,revision}," in
  *,gitops,*|*,campaign,*|*,openspec,*|*,automation,*)
    if gh auth status >/dev/null 2>&1; then
      echo "[run_workers] GitHub authentication preflight passed"
    else
      echo "[run_workers] WARNING: GitHub authentication unavailable; configure gh auth login or GH_TOKEN before running GitHub-backed workflows" >&2
    fi
    ;;
esac
child_pid=""

terminate_descendants() {
  local parent_pid="$1"
  local descendant_pid
  # TaskHandler is a process tree: supervisor -> main -> one poller per task
  # type. ``pkill -P`` only reaches one level, leaving orphan pollers after a
  # reload and recreating the duplicate-poll storm we are trying to prevent.
  for descendant_pid in $(pgrep -P "$parent_pid" 2>/dev/null || true); do
    terminate_descendants "$descendant_pid"
    kill -TERM "$descendant_pid" 2>/dev/null || true
  done
}

terminate_generation() {
  local leader_pid="$1"
  # main.py creates a process group before TaskHandler spawns pollers. Killing
  # that group remains reliable even after main.py exits and its children have
  # already been reparented to PID 1.
  kill -TERM -- "-$leader_pid" 2>/dev/null || true
  terminate_descendants "$leader_pid"
}

stop_poller() {
  if [[ -n "$child_pid" ]]; then
    # Stop the TaskHandler first so its monitor cannot restart a child while
    # the supervisor is shutting down, then terminate every remaining level.
    terminate_generation "$child_pid"
    sleep 0.2
    wait "$child_pid" 2>/dev/null || true
  fi
  exit 0
}

trap stop_poller INT TERM

while true; do
  "$PY" main.py &
  child_pid=$!
  wait "$child_pid"
  code=$?
  # A crashed/reloaded TaskHandler can leave pollers reparented to PID 1. The
  # generation process group gives the supervisor a stable cleanup target.
  terminate_generation "$child_pid"
  child_pid=""
  if [[ "$code" -eq 75 ]]; then
    echo "[run_workers] duplicate worker deployment refused — supervisor exiting"
    exit 0
  fi
  echo "[run_workers] poller exited (code $code) — restarting in ${DELAY}s"
  sleep "$DELAY"
done
