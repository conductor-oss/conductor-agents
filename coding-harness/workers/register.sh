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

# Same key/secret-or-static-token precedence as workers/automation/tasks.py's
# _conductor_token: an explicit token wins outright; otherwise exchange
# key/secret for one; otherwise no auth header (open/local server). A 404 from
# /token means the server has no auth endpoint, so no auth is required.
echo "[register] resolving authentication…"
AUTH_TOKEN="${CONDUCTOR_AUTH_TOKEN:-}"
if [ -z "$AUTH_TOKEN" ] && [ -n "${CONDUCTOR_AUTH_KEY:-}" ] && [ -n "${CONDUCTOR_AUTH_SECRET:-}" ]; then
  token_response=$(curl -s -w '\n%{http_code}' -X POST "$CONDUCTOR_SERVER_URL/token" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg k "$CONDUCTOR_AUTH_KEY" --arg s "$CONDUCTOR_AUTH_SECRET" '{keyId: $k, keySecret: $s}')")
  token_status="${token_response##*$'\n'}"
  if [ "$token_status" = "404" ]; then
    echo "[register] server has no /token endpoint; no auth required"
  else
    AUTH_TOKEN=$(jq -r '.token // empty' <<<"${token_response%$'\n'"$token_status"}" 2>/dev/null || true)
    if [ -z "$AUTH_TOKEN" ]; then
      echo "[register] ERROR: failed to obtain a Conductor auth token from CONDUCTOR_AUTH_KEY/SECRET (HTTP $token_status)" >&2
      exit 1
    fi
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
# Sub-workflows first, so a parent is registered after the children it starts.
WORKFLOW_ORDER=(design_docs document_plan test_agent_fallback test_cycle merge_remediation openspec_generate_artifact openspec_artifact_drain openspec_plan campaign_subtask code_revision_loop code_subtask code_parallel pr_draft_approval dag_plan_approval implementation_waves final_verification feature_campaign openspec_development github_demo local_review publish_salvage issue_to_pr publish_verified_pr address_pr_repair address_pr_approval address_pr pr_review automation_reset automation_dispatch pr_review_sweep pr_address_sweep issue_resolution_sweep runtime_health)
workflow_files=()
for wf in "${WORKFLOW_ORDER[@]}"; do
  workflow_files+=("workflows/$wf.json")
done
# A workflow file not in WORKFLOW_ORDER is registered after the ordered ones.
for f in workflows/*.json; do
  name=$(basename "$f" .json)
  listed=false
  for wf in "${WORKFLOW_ORDER[@]}"; do
    if [ "$wf" = "$name" ]; then
      listed=true
      break
    fi
  done
  if [ "$listed" != true ]; then
    WORKFLOW_ORDER+=("$name")
    workflow_files+=("$f")
  fi
done

jq -s '.' "${workflow_files[@]}" | api PUT /metadata/workflow
for wf in "${WORKFLOW_ORDER[@]}"; do
  version=$(jq -r '.version' "workflows/$wf.json")
  echo "  $wf v$version"
done

# api_read METHOD /path -- bodyless GET/DELETE that echoes the response body and
# fails loudly like api() does. api() cannot serve these: it always reads a
# request body from stdin.
api_read() {
  local method="$1" path="$2"
  local -a headers=(-H "Accept: application/json")
  [ -n "$AUTH_TOKEN" ] && headers+=(-H "X-Authorization: $AUTH_TOKEN")
  local response status body
  response=$(curl -s -w '\n%{http_code}' -X "$method" "${headers[@]}" \
    "$CONDUCTOR_SERVER_URL$path")
  status="${response##*$'\n'}"
  body="${response%$'\n'"$status"}"
  if [ "$status" -ge 400 ]; then
    echo "[register] ERROR: $method $path -> HTTP $status" >&2
    echo "$body" >&2
    exit 1
  fi
  printf '%s' "$body"
}

# live_executions NAME VERSION -- count of executions that must keep running on
# an obsolete definition.
live_executions() {
  local name="$1" version="$2"
  local -a headers=(-H "Accept: application/json")
  [ -n "$AUTH_TOKEN" ] && headers+=(-H "X-Authorization: $AUTH_TOKEN")
  curl -s -G "${headers[@]}" "$CONDUCTOR_SERVER_URL/workflow/search" \
    --data "start=0&size=1000&freeText=*" \
    --data-urlencode "query=workflowType='$name' AND status IN (RUNNING,PAUSED)" \
    | jq --argjson v "$version" '[.results[]? | select(.version == $v)] | length'
}

echo "[register] pruning superseded workflow versions…"
# Workflows are started without a version, so the server runs the HIGHEST
# registered version. The bulk PUT above upserts the local version but cannot
# remove another one left behind by an older definition, and Conductor resolves an unversioned
# `POST /workflow/{name}` to the HIGHEST registered version. An orphan therefore
# silently shadows every local change: a stale feature_campaign v2 whose
# campaign_workspace never wired `repoUrl` won over the current v1 and failed
# every clone-from-GitHub campaign at workspace_prepare. Pruning here is what
# keeps the local definition the one that actually runs.
pruned=0
for wf in "${WORKFLOW_ORDER[@]}"; do
  local_version=$(jq -r '.version // 1' "workflows/$wf.json")
  obsolete=$(api_read GET "/metadata/workflow/$wf/versions" \
    | jq -r --argjson v "$local_version" '.[]? | select(.version != $v) | .version')
  for version in $obsolete; do
    live=$(live_executions "$wf" "$version")
    if [ "$live" != "0" ]; then
      echo "  kept $wf v$version: $live RUNNING/PAUSED execution(s) still use it"
      continue
    fi
    api_read DELETE "/metadata/workflow/$wf/$version" >/dev/null
    echo "  removed $wf v$version (superseded by v$local_version)"
    pruned=$((pruned + 1))
  done
done
if [ "$pruned" -eq 0 ]; then
  echo "  none (every workflow has only its local version registered)"
fi

echo "[register] complete"
