#!/bin/bash
# Register every task definition and workflow with Conductor. Safe to rerun:
# every definition is upserted (created if missing, updated if present).
#
# Registers via direct REST (curl), never the `conductor` CLI: the CLI parses
# a taskdef/workflowdef into its own internal model and re-serializes from
# that, which silently drops fields it doesn't know about -- confirmed with
# inputSchema/outputSchema on both `task create/update` and `workflow
# create/update`, even on the latest CLI release. A definition registered
# through the CLI has no input/output schema on the server even though the
# local JSON file has one, and nothing here or in the CLI's own output says
# so. Bulk REST endpoints preserve every field exactly as written.
set -euo pipefail
WORKERS_DIR=$(cd "$(dirname "$0")" && pwd)
HARNESS_ROOT=$(cd "$WORKERS_DIR/.." && pwd)
# shellcheck disable=SC1091
. "$HARNESS_ROOT/scripts/conductor_env.sh"
load_harness_environment "$HARNESS_ROOT/.env"
cd "$WORKERS_DIR"

command -v jq >/dev/null 2>&1 || {
  echo "[register] ERROR: jq is required to validate and register definitions" >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || {
  echo "[register] ERROR: curl is required to register definitions" >&2
  exit 1
}

echo "[register] validating versioned workflow references…"
invalid_workflow_versions=$(jq -r 'select(.version != 1) | .name + " v" + (.version | tostring)' workflows/*.json)
if [ -n "$invalid_workflow_versions" ]; then
  echo "[register] ERROR: every workflow must be version 1:" >&2
  echo "$invalid_workflow_versions" >&2
  exit 1
fi
invalid_subworkflow_versions=$(jq -s -r '
  (map({key: .name, value: .version}) | from_entries) as $versions
  | .[] | .. | objects
  | select(.type? == "SUB_WORKFLOW")
  | select($versions[.subWorkflowParam.name] == null or
           .subWorkflowParam.version != $versions[.subWorkflowParam.name])
  | .taskReferenceName + " -> " + .subWorkflowParam.name + " v" +
    (.subWorkflowParam.version | tostring) + " (current v" +
    (($versions[.subWorkflowParam.name] // "missing") | tostring) + ")"
' workflows/*.json)
if [ -n "$invalid_subworkflow_versions" ]; then
  echo "[register] ERROR: SUB_WORKFLOW reference does not pin the current local definition:" >&2
  echo "$invalid_subworkflow_versions" >&2
  exit 1
fi
invalid_dynamic_subworkflow_versions=$(jq -s -r '
  (map({key: .name, value: .version}) | from_entries) as $versions
  | .[] | .. | strings
  | (try capture("subWorkflowParam:\\{name:\"(?<name>[^\"]+)\", version:(?<version>[0-9]+)") catch null)
  | select(. != null)
  | select($versions[.name] == null or (.version | tonumber) != $versions[.name])
  | .name + " v" + .version + " (current v" +
    (($versions[.name] // "missing") | tostring) + ")"
' workflows/*.json)
if [ -n "$invalid_dynamic_subworkflow_versions" ]; then
  echo "[register] ERROR: dynamically generated SUB_WORKFLOW reference is stale:" >&2
  echo "$invalid_dynamic_subworkflow_versions" >&2
  exit 1
fi

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

# Same key/secret-or-static-token precedence as workers/automation/tasks.py's
# _conductor_token: an explicit token wins outright; otherwise exchange
# key/secret for one; otherwise no auth header (open/local server).
echo "[register] resolving authentication…"
AUTH_TOKEN="${CONDUCTOR_AUTH_TOKEN:-}"
if [ -z "$AUTH_TOKEN" ] && [ -n "${CONDUCTOR_AUTH_KEY:-}" ] && [ -n "${CONDUCTOR_AUTH_SECRET:-}" ]; then
  AUTH_TOKEN=$(curl -s -X POST "$CONDUCTOR_SERVER_URL/token" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg k "$CONDUCTOR_AUTH_KEY" --arg s "$CONDUCTOR_AUTH_SECRET" '{keyId: $k, keySecret: $s}')" \
    | jq -r '.token // empty')
  if [ -z "$AUTH_TOKEN" ]; then
    echo "[register] ERROR: failed to obtain a Conductor auth token from CONDUCTOR_AUTH_KEY/SECRET" >&2
    exit 1
  fi
fi

# api PUT|POST /path <<<"$json_body" -- fails loudly with the server's own
# response body on any non-2xx or transport error, never silently.
api() {
  local method="$1" path="$2"
  local -a headers=(-H "Content-Type: application/json")
  [ -n "$AUTH_TOKEN" ] && headers+=(-H "X-Authorization: $AUTH_TOKEN")
  local response status body
  response=$(curl -s -w '\n%{http_code}' -X "$method" "${headers[@]}" \
    "$CONDUCTOR_SERVER_URL$path" --data-binary @-)
  status="${response##*$'\n'}"
  body="${response%$'\n'"$status"}"
  if [ "$status" -ge 400 ]; then
    echo "[register] ERROR: $method $path -> HTTP $status" >&2
    echo "$body" >&2
    exit 1
  fi
}

echo "[register] task definitions (bulk upsert)…"
# responseTimeoutSeconds: 0 is this repository's own deliberate "no hidden
# execution timeout" convention (see AGENTS.md's Timeout Policy) and stays 0
# on disk. Some Conductor deployments reject 0 at registration time
# ("should be minimum 1 second"); the override below applies only to the
# outgoing payload. It has no effect on actual task behavior since
# CONDUCTOR_WORKER_ALL_LEASE_EXTEND_ENABLED already keeps a lease alive for
# the task's real, unbounded duration regardless of this value.
jq -s 'map(if .responseTimeoutSeconds == 0 then .responseTimeoutSeconds = 3600 else . end)' \
  workflows/taskdefs/*.json | api POST /metadata/taskdefs
for f in workflows/taskdefs/*.json; do
  echo "  $(jq -r '.name' "$f")"
done

echo "[register] workflows (bulk upsert, sub-workflows first)…"
# Sub-workflows must be available before workflows that pin their version.
WORKFLOW_ORDER=(design_docs document_plan test_agent_fallback test_cycle merge_remediation openspec_generate_artifact openspec_artifact_drain openspec_plan campaign_subtask code_revision_loop code_subtask code_parallel feature_campaign openspec_development github_demo local_review publish_salvage issue_to_pr publish_verified_pr address_pr_repair address_pr_approval address_pr pr_review automation_reset automation_dispatch pr_review_sweep pr_address_sweep issue_resolution_sweep runtime_health)
workflow_files=()
for wf in "${WORKFLOW_ORDER[@]}"; do
  workflow_files+=("workflows/$wf.json")
done
jq -s '.' "${workflow_files[@]}" | api PUT /metadata/workflow
for wf in "${WORKFLOW_ORDER[@]}"; do
  version=$(jq -r '.version' "workflows/$wf.json")
  echo "  $wf v$version"
done

echo "[register] worker gate…"
get_status() {
  # bash 3.2 (macOS's default) treats "${arr[@]}" on an EMPTY array as an
  # unbound variable under `set -u`, so this array is never left empty.
  local -a headers=(-H "Accept: application/json")
  [ -n "$AUTH_TOKEN" ] && headers+=(-H "X-Authorization: $AUTH_TOKEN")
  curl -s -o /dev/null -w '%{http_code}' "${headers[@]}" "$CONDUCTOR_SERVER_URL$1"
}
for task_name in $simple_tasks; do
  status=$(get_status "/metadata/taskdefs/$task_name")
  if [ "$status" != "200" ]; then
    echo "[register] ERROR: GET /metadata/taskdefs/$task_name -> HTTP $status" >&2
    exit 1
  fi
  echo "  $task_name registered"
done
echo "[register] complete"
