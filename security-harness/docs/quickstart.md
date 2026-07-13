# Quickstart

Get a scan running in about 30 seconds. `./scan` auto-starts the full Conductor stack for you.

!!! warning "Authorized testing only"
    Only scan systems you **own or have explicit written permission to test.** See [Authorization & capability levels](authorization.md).

## Prerequisites

- `conductor` CLI, Docker, Python 3.11+, `jq`, `curl`
- `ANTHROPIC_API_KEY` — export it before your first run; `./scan`/`./assess` auto-start the Conductor server and it picks the key up from the environment
- Run `make venv` once to build the worker virtualenv (installs Python deps + Playwright Chromium)

## One-time setup

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make venv                             # build worker venv — do this once
make up                               # start OWASP Juice Shop (an intentionally vulnerable demo app, safe to scan) on :3001
```

## First scan

Then scan it — **`./scan` auto-starts the full Conductor stack for you**:

```bash
./scan http://localhost:3001 --authorized
#   ℹ  conductor server: starting …        ← auto-started if not already up
#   ℹ  workflows: registering …            ← auto-registered if missing
#   ℹ  workers: starting fleet …           ← auto-started if not already polling
#   → http://localhost:8080/execution/<id>  (watch it live in the Conductor UI)
#   → reports/<scan-id>/report.pdf + report.md + findings.json + report.sarif
```

When you're done: `make down` tears everything down.

!!! note "CI / shared server"
    Pass `--no-bootstrap` (or `SC_NO_BOOTSTRAP=1`) to skip auto-start and manage the stack yourself:

    ```bash
    make server && make register && make workers
    ```

## The authorization gate

Without `--authorized`, the workflow stops at the authorization gate by design — this is intentional:

```bash
./scan http://localhost:3001          # → TERMINATED at authorization_gate
./scan http://localhost:3001 --authorized
```

See [Authorization & capability levels](authorization.md) for `--manifest` and capability ceilings.

## Try it on the bundled vulnerable app

A tiny deliberately-vulnerable Express app ships in `examples/vuln-app/` — a quick way to exercise the harness end to end. It has **no authentication** and seeds OS command injection (`/ping`), code injection (`/calc`), a SQL-injection sink (`/search`), a no-authz `/admin/users` endpoint, and hardcoded secrets.

```bash
# Terminal 1 — launch the test app (serves on :3000; set PORT to change it)
cd examples/vuln-app && npm install && node server.js

# Terminal 2 — deep assess WITH --source (recommended for this app):
./assess http://localhost:3000 --authorized --capability 2 --profile vuln-app --source examples/vuln-app
```

!!! tip "Use `--assess --source`, not `./scan`, for this app"
    Its routes aren't linked from `/`, and `./scan` is crawl-driven — it discovers 0 endpoints here and only reports HTTP-header hygiene. `./assess --source` runs route extraction over `server.js` to derive `/ping`, `/calc`, `/search` and then actively tests them, so it catches the command/code injection. SAST findings (the hardcoded secrets, the SQLi sink) additionally require `semgrep`/`gitleaks` on `PATH` (see [Deployment modes](deployment.md)); they degrade gracefully if absent.

!!! warning "The vuln-app is intentionally insecure"
    Bind it to localhost only and never expose it. The `--profile vuln-app` hints the expected finding classes; the engine works without it.
