# Sast, Scan & Assess

Three entry points for different testing depths. (`./run.sh <url> [flags]` is a convenience alias that forwards to `./scan`/`./assess`.)

| | `./sast` — `sast_report` workflow | `./scan` — `security_scan` workflow | `./assess` — `deep_assess` workflow |
|---|---|---|---|
| **Needs a live site?** | **No** — source code only | Yes | Yes |
| **Purpose** | Source-only static analysis: SAST (semgrep/gitleaks/trivy) + route extraction → triage → report | Fast surface scan: crawl → plan → DAST → triage → report | Deep agentic pentest: understand → hypothesize → **actively exploit** → adversarially verify, over iterative-deepening passes |
| **Confirms bugs?** | No live target — reports **triaged** static findings (LLM cuts false positives; no active/out-of-band confirmation) | Reports candidates (+ active DAST hits) | **Yes** — re-runs each PoC and confirms blind bugs out-of-band; multi-identity cross-tenant testing |
| **Typical time** | ~1–2 min | ~3–5 min | Tens of minutes |
| **Authorization** | None needed (reads local source only; no network) | `--authorized` for quick tests; `--manifest` for real engagements | Same; `--manifest` recommended for anything beyond local demo |
| **Capability** | n/a (no active requests) | read / active DAST (`--intrusive`) | 0–4; product-feature exploitation needs `--capability 2` |

## `./sast` examples

Point it at code on disk — no server, no target, no `--authorized` (it only reads local files):

```bash
# Fast single-pass triage (~1-2 min) → reports/<scan-id>/report.{md,pdf} + findings.json + report.sarif
./sast ./path/to/repo

# --deep: a code-reading agent investigates EACH candidate (reads/greps the source to judge
# reachability, refute-by-default) — cuts false positives hard. Slower, more LLM calls.
./sast ./path/to/repo --deep

# Label the run; reuse an already-running stack:
./sast ./path/to/repo --target "acme-api @ abcd123" --no-bootstrap

# Or via make:
make sast-report SRC=./path/to/repo            # add ARGS='--deep' for the verified pass
```

!!! note "`./sast` is static only"
    It runs the analyzers and an LLM triage pass (which cuts false positives), but it does **not** crawl, actively exploit, or confirm anything against a running app — there is no live target. With `--deep` it does two things with read-only code-reading agents: it **verifies** each scanner finding (is it reachable from untrusted input? drop dead/test/sanitized), and it **hunts** the source for exploitable attack chains the scanners miss (untrusted input → dangerous sink, including the dependency/Log4Shell class), adversarially refuting each — the same "prove it or drop it" discipline as the live scan, applied statically. When the app is also running, `./assess --source --hunt` feeds those hunted chains into the live exploit → verify loop for **dynamic (out-of-band) confirmation**.

## `./scan` examples

```bash
# Passive + active surface scan:
./scan http://localhost:3001 --authorized

# Authenticated scan — tests the API behind auth (IDOR/BOLA/SSRF), not just maps it:
./scan https://app.example.com --authorized \
  --auth-key "$KEY" --auth-secret "$SECRET" --token-url https://app.example.com/api/token \
  --scope app.example.com

# With source code — adds SAST + route extraction to seed the live scan:
./scan https://app.example.com --authorized --source ./code --intrusive

# Already-running stack (skip auto-start):
./scan http://localhost:3001 --authorized --no-bootstrap
```

## `./assess` examples

```bash
# Two identities unlock BOLA / privilege-escalation / cross-tenant tests:
./assess https://app.example.com --authorized \
  --id 'userA=token:eyJ...A' --id 'userB=token:eyJ...B' --scope app.example.com

# Capability-2 deep pentest — docs + source + two tenants + purple team:
./assess https://your-conductor.example.com --authorized --capability 2 \
  --profile conductor --docs https://orkes.io/content/ --source /path/to/source \
  --id 'orgA=key:K1,secret:S1,tokenurl:https://your-conductor.example.com/api/token,tenant:orgA' \
  --id 'orgB=key:K2,secret:S2,tokenurl:https://your-conductor.example.com/api/token,tenant:orgB' \
  --purple --resilience --require-tenants 2 --leave-evidence

# Pin to a specific objective:
./assess https://app.example.com --authorized --capability 2 --objective INFRA-SSRF

# Hunt the source for exploitable chains, then dynamically confirm them against the live app:
./assess https://app.example.com --authorized --capability 2 --source ./code --hunt \
  --id 'userA=token:...' --oob <collaborator>
```

!!! note "Capability-2 prerequisites"
    Build the sandbox image once with `make codeexec-image`. For blind SSRF/RCE/exfil confirmation, start an OOB collaborator with `make oob`. `./assess` preflights and refuses a cap-2 run if the sandbox image is missing.

See [Authorization & capability levels](authorization.md) for what each capability level permits, and [Authentication & SSO](authentication.md) for supplying credentials and identities.
