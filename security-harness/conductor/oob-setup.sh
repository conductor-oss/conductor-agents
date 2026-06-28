#!/usr/bin/env bash
# Stand up the OOB collaborator: a local listener + a cloudflared quick-tunnel that
# gives it a public HTTPS URL the target can reach inbound. Writes a state file
# (workers/oob/state.json) that the `assess` CLI reads (-> oob_base passed into the
# run) and the oob_check worker reads (-> local port to query hits).
#
#   bash conductor/oob-setup.sh         # start (idempotent-ish; stop first if running)
#   bash conductor/oob-setup.sh --stop  # tear down
#
# Requires: cloudflared (brew install cloudflared). No account/login needed for a
# quick tunnel; the URL is ephemeral and changes each start.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${SC_OOB_PORT:-8099}"
STATE="$ROOT/workers/oob/state.json"
LIST_LOG="/tmp/sc-oob-listener.log"
CF_LOG="/tmp/sc-oob-cloudflared.log"
PY="$ROOT/workers/.venv/bin/python"

stop() {
  [ -f "$STATE" ] || { echo "no OOB state; nothing to stop"; return 0; }
  for k in listener_pid cloudflared_pid; do
    pid="$(jq -r ".$k // empty" "$STATE" 2>/dev/null || true)"
    [ -n "$pid" ] && kill "$pid" 2>/dev/null && echo "stopped $k=$pid" || true
  done
  rm -f "$STATE"
  echo "OOB collaborator stopped."
}

if [ "${1:-}" = "--stop" ]; then stop; exit 0; fi
command -v cloudflared >/dev/null || { echo "ERROR: cloudflared not installed (brew install cloudflared)" >&2; exit 1; }
[ -x "$PY" ] || PY="python3"

# Fresh start.
[ -f "$STATE" ] && stop || true

echo "→ starting OOB listener on 127.0.0.1:$PORT"
"$PY" "$ROOT/workers/oob/listener.py" >"$LIST_LOG" 2>&1 &
LIST_PID=$!
sleep 1
kill -0 "$LIST_PID" 2>/dev/null || { echo "ERROR: listener failed to start:" >&2; cat "$LIST_LOG" >&2; exit 1; }

echo "→ starting cloudflared quick-tunnel -> http://localhost:$PORT"
# trycloudflare's account-less quick-tunnel API intermittently 500s ("error code:
# 1101"); it's transient, so retry the whole tunnel a few times.
BASE=""; CF_PID=""
for attempt in 1 2 3 4 5 6; do
  : >"$CF_LOG"
  cloudflared tunnel --url "http://localhost:$PORT" --no-autoupdate >"$CF_LOG" 2>&1 &
  CF_PID=$!
  for _ in $(seq 1 15); do
    BASE="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG" 2>/dev/null | head -1 || true)"
    [ -n "$BASE" ] && break
    kill -0 "$CF_PID" 2>/dev/null || break    # cloudflared exited (likely 1101) -> retry
    sleep 1
  done
  [ -n "$BASE" ] && break
  echo "  tunnel attempt $attempt failed ($(grep -oE 'error code: [0-9]+' "$CF_LOG" | head -1 || echo 'no url')); retrying"
  kill "$CF_PID" 2>/dev/null || true; CF_PID=""; sleep 3
done
[ -n "$BASE" ] || { echo "ERROR: could not obtain tunnel URL after retries:" >&2; tail -20 "$CF_LOG" >&2; kill "$LIST_PID" 2>/dev/null; exit 1; }

jq -n --arg b "$BASE" --argjson p "$PORT" --argjson lp "$LIST_PID" --argjson cp "$CF_PID" \
  '{public_base_url:$b, local_port:$p, listener_pid:$lp, cloudflared_pid:$cp}' > "$STATE"

echo "✓ OOB collaborator ready"
echo "  public base: $BASE   (canaries: $BASE/c/<token>)"
echo "  local query: http://127.0.0.1:$PORT/_oob/hits?token=<token>"
echo "  state: $STATE   (stop with: bash conductor/oob-setup.sh --stop)"
