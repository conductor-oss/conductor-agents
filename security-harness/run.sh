#!/usr/bin/env bash
# conductor-agents / security-harness — convention entrypoint.
#
#   ./run.sh                 Run the bundled local demo: scan the OWASP Juice Shop target on :3001.
#                            (Start the target first with `make up`.)
#   ./run.sh scan   ARGS...  Forward to ./scan   — fast surface scan (workflow: security_scan).
#   ./run.sh assess ARGS...  Forward to ./assess — deep agentic pentest (workflow: deep_assess).
#   ./run.sh <url>  ARGS...  Shorthand for `./run.sh scan <url> ARGS...`.
#
# ./scan and ./assess auto-bootstrap the stack (server -> register -> worker fleet). For a cap-2
# product-exploitation run, build the sandbox once with `make codeexec-image` and (for blind
# SSRF/RCE confirmation) start an OOB collaborator with `make oob`. AUTHORIZED TARGETS ONLY.
set -euo pipefail
cd "$(dirname "$0")"

if [ $# -eq 0 ]; then
  echo "▶ demo: scanning the local OWASP Juice Shop target (http://localhost:3001)"
  echo "  (if it isn't running yet: make up)"
  exec ./scan http://localhost:3001 --authorized
fi

case "$1" in
  scan)   shift; exec ./scan "$@" ;;
  assess) shift; exec ./assess "$@" ;;
  -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  *)      exec ./scan "$@" ;;
esac
