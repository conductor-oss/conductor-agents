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
# shellcheck disable=SC1091
. "$HARNESS_ROOT/scripts/conductor_env.sh"
load_harness_environment "$HARNESS_ROOT/.env" || exit $?
cd "$WORKERS_DIR"
PY=.venv/bin/python
DELAY=5
echo "[run_workers] CONDUCTOR_SERVER_URL=$CONDUCTOR_SERVER_URL modules=${WORKER_MODULES:-<all>}"
while true; do
  "$PY" main.py
  code=$?
  echo "[run_workers] poller exited (code $code) — restarting in ${DELAY}s"
  sleep "$DELAY"
done
