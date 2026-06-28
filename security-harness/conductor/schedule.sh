#!/usr/bin/env bash
# Create (or update) a recurring security_scan schedule.
#   conductor/schedule.sh <url> [cron] [source_path]
# Default cron: daily at 02:00 (Quartz: seconds minutes hours day month dow).
set -euo pipefail

URL="${1:?usage: schedule.sh <url> [cron] [source_path]}"
CRON="${2:-0 0 2 * * ?}"
SRC="${3:-}"
NAME="scan-$(printf '%s' "$URL" | sed 's#[^a-zA-Z0-9]#-#g' | cut -c1-40)"

input=$(jq -n --arg url "$URL" --arg src "$SRC" '
  {target: $url, authorized: true} + (if $src == "" then {} else {source_path: $src} end)')

sched=$(jq -n --arg name "$NAME" --arg cron "$CRON" --argjson input "$input" '{
  name: $name,
  cronExpression: $cron,
  startWorkflowRequest: { name: "security_scan", version: 1, input: $input },
  scheduleStartTime: 0, scheduleEndTime: 0, paused: false
}')

tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
printf '%s' "$sched" > "$tmp"
conductor schedule create "$tmp" >/dev/null 2>&1 \
  && echo "✓ created schedule '$NAME'  cron='$CRON'  target=$URL" \
  || { conductor schedule update "$tmp" >/dev/null && echo "✓ updated schedule '$NAME'"; }
echo "  manage: conductor schedule pause|resume|delete $NAME"
