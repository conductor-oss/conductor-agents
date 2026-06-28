#!/usr/bin/env bash
# Register task definitions + workflows with the configured Conductor server.
# System prompts in prompts/*.md are injected into the LLM tasks at register
# time so prompts/ stays the single source of truth.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASKDEFS="$ROOT/conductor/taskdefs"
WORKFLOWS="$ROOT/conductor/workflows"
PROMPTS="$ROOT/prompts"
KB="$ROOT/knowledge/owasp-remediation.md"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

: "${CONDUCTOR_SERVER_URL:=http://localhost:8080/api}"
echo "→ Conductor server: $CONDUCTOR_SERVER_URL"

command -v conductor >/dev/null || { echo "ERROR: conductor CLI not found on PATH"; exit 1; }
command -v jq >/dev/null || { echo "ERROR: jq is required to inject prompts"; exit 1; }

echo "── Task definitions ───────────────────────────────"
for f in "$TASKDEFS"/*.json; do
  [ -e "$f" ] || continue
  name=$(jq -r '.name' "$f")
  if conductor task create "$f" >/dev/null 2>&1; then
    echo "  ✓ taskDef $name (created)"
  elif conductor task update "$f" >/dev/null 2>&1; then
    echo "  ✓ taskDef $name (updated)"
  else
    echo "  ✗ taskDef $name — registration FAILED:" >&2
    conductor task create "$f" 2>&1 | sed 's/^/      /' >&2
    exit 1
  fi
done

echo "── Workflows ──────────────────────────────────────"
inject_prompts() {
  # $1 = source workflow json, $2 = dest built json
  # Every LLM system prompt is prefixed with the self-defense guardrail ($guard) so
  # untrusted target content can never override scope/policy/authorization (spec 23).
  jq \
    --rawfile guard "$PROMPTS/_guardrail.md" \
    --rawfile triage "$PROMPTS/triage.md" \
    --rawfile report "$PROMPTS/report.md" \
    --rawfile plan "$PROMPTS/plan.md" \
    --rawfile agent "$PROMPTS/agent.md" \
    --rawfile kb "$KB" \
    --rawfile appmodel "$PROMPTS/app_model.md" \
    --rawfile explore "$PROMPTS/explore.md" \
    --rawfile hypothesize "$PROMPTS/hypothesize.md" \
    --rawfile exploit "$PROMPTS/exploit.md" \
    --rawfile deepen "$PROMPTS/exploit_deepen.md" \
    --rawfile verify "$PROMPTS/verify.md" \
    --rawfile docsdigest "$PROMPTS/docs_digest.md" \
    --rawfile reflect "$PROMPTS/reflect.md" \
    --rawfile purple "$PROMPTS/purple.md" '
      (.tasks[]? | select(.taskReferenceName=="triage").inputParameters.messages[0].message) = ($guard + $triage + "\n\n---\n" + $kb)
      | (.tasks[]? | select(.taskReferenceName=="report_md").inputParameters.messages[0].message) = ($guard + $report)
      | (.tasks[]? | select(.taskReferenceName=="plan").inputParameters.messages[0].message) = ($guard + $plan)
      | (.tasks[]? | select(.taskReferenceName=="agent_init").inputParameters.messages[0].message) = ($guard + $agent)
      | (.tasks[]? | select(.taskReferenceName=="app_model").inputParameters.messages[0].message) = ($guard + $appmodel)
      | (.tasks[]? | select(.taskReferenceName=="explore_init").inputParameters.messages[0].message) = ($guard + $explore)
      | (.tasks[]? | select(.taskReferenceName=="hypothesize").inputParameters.messages[0].message) = ($guard + $hypothesize)
      | (.tasks[]? | select(.taskReferenceName=="exploit_init").inputParameters.messages[0].message) = ($guard + $exploit)
      | (.tasks[]? | select(.taskReferenceName=="deepen_exploit_init").inputParameters.messages[0].message) = ($guard + $deepen)
      | (.tasks[]? | select(.taskReferenceName=="verify").inputParameters.messages[0].message) = ($guard + $verify)
      | (.tasks[]? | select(.taskReferenceName=="docs_digest").inputParameters.messages[0].message) = ($guard + $docsdigest)
      | (.tasks[]? | select(.taskReferenceName=="reflect").inputParameters.messages[0].message) = ($guard + $reflect)
      | (.tasks[]? | select(.taskReferenceName=="purple_check").inputParameters.messages[0].message) = ($guard + $purple)
    ' "$1" > "$2"
}

for wf in "$WORKFLOWS"/*.json; do
  [ -e "$wf" ] || continue
  name=$(jq -r '.name' "$wf")
  built="$BUILD/$(basename "$wf")"
  # Only the main scan workflow carries triage/report/plan prompts; the jq
  # filter is a no-op for workflows that lack those task refs.
  if [ -f "$PROMPTS/_guardrail.md" ] && [ -f "$PROMPTS/triage.md" ] && [ -f "$PROMPTS/report.md" ] && [ -f "$PROMPTS/plan.md" ] && [ -f "$PROMPTS/agent.md" ] && [ -f "$KB" ] && [ -f "$PROMPTS/app_model.md" ] && [ -f "$PROMPTS/explore.md" ] && [ -f "$PROMPTS/hypothesize.md" ] && [ -f "$PROMPTS/exploit.md" ] && [ -f "$PROMPTS/exploit_deepen.md" ] && [ -f "$PROMPTS/verify.md" ] && [ -f "$PROMPTS/docs_digest.md" ] && [ -f "$PROMPTS/reflect.md" ] && [ -f "$PROMPTS/purple.md" ]; then
    inject_prompts "$wf" "$built"
  else
    cp "$wf" "$built"
  fi
  if conductor workflow create "$built" >/dev/null 2>&1; then
    echo "  ✓ workflow $name (created)"
  elif conductor workflow update "$built" >/dev/null 2>&1; then
    echo "  ✓ workflow $name (updated)"
  else
    echo "  ✗ workflow $name — registration FAILED:" >&2
    conductor workflow update "$built" 2>&1 | sed 's/^/      /' >&2
    exit 1
  fi
done

echo "── Done. Registered task defs + workflows. ────────"
