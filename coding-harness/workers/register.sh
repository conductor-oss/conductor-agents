#!/bin/bash
# Register every task definition and workflow with Conductor. Safe to rerun:
# existing definitions are updated; missing definitions are created.
set -euo pipefail
WORKERS_DIR=$(cd "$(dirname "$0")" && pwd)
HARNESS_ROOT=$(cd "$WORKERS_DIR/.." && pwd)
# shellcheck disable=SC1091
. "$HARNESS_ROOT/scripts/conductor_env.sh"
load_harness_environment "$HARNESS_ROOT/.env"
cd "$WORKERS_DIR"

command -v conductor >/dev/null 2>&1 || {
  echo "[register] ERROR: conductor CLI is not installed" >&2
  exit 1
}
command -v jq >/dev/null 2>&1 || {
  echo "[register] ERROR: jq is required to validate workflow definitions" >&2
  exit 1
}

echo "[register] validating SIMPLE task definitions…"
simple_tasks=$(jq -r '.. | objects | select(.type? == "SIMPLE") | .name' workflows/*.json | sort -u)
for task_name in $simple_tasks; do
  found=false
  for f in workflows/taskdefs/*.json; do
    if [ "$(jq -r '.name' "$f")" = "$task_name" ]; then
      found=true
      break
    fi
  done
  if [ "$found" != true ]; then
    echo "[register] ERROR: SIMPLE task '$task_name' has no local task definition" >&2
    exit 1
  fi
done

echo "[register] task definitions…"
for f in workflows/taskdefs/*.json; do
  name=$(jq -r '.name' "$f")
  if conductor task get "$name" >/dev/null 2>&1; then
    conductor task update "$f" >/dev/null
    action=updated
  else
    conductor task create "$f" >/dev/null
    action=created
  fi
  conductor task get "$name" >/dev/null
  echo "  $name ($action)"
done

echo "[register] workflows (sub-workflows first)…"
# Sub-workflows must be available before workflows that pin their version.
for wf in openspec_generate_artifact openspec_artifact_drain openspec_plan campaign_subtask code_revision_loop code_subtask code_parallel feature_campaign openspec_development github_demo local_review issue_to_pr address_pr pr_review automation_reset automation_dispatch pr_review_sweep pr_address_sweep issue_resolution_sweep; do
  f="workflows/$wf.json"
  version=$(jq -r '.version' "$f")
  if conductor workflow get "$wf" >/dev/null 2>&1; then
    conductor workflow update "$f" >/dev/null
    action=updated
  else
    conductor workflow create "$f" >/dev/null
    action=created
  fi
  conductor workflow get "$wf" >/dev/null
  echo "  $wf v$version ($action)"
done

echo "[register] worker gate…"
for task_name in $simple_tasks; do
  conductor task get "$task_name" >/dev/null
  echo "  $task_name registered"
done
echo "[register] complete"
