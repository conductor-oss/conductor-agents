## Why

The coding-harness's default local Conductor server (`conductor server start`) is SQLite-backed. While debugging an `openspec_plan` run, its `FORK_JOIN_DYNAMIC` artifact fan-out coincided with the SQLite backend throwing `NonTransientException: [SQLITE_BUSY]` / `SQLITE_BUSY_SNAPSHOT` on a workflow state transition — confirmed live against a real Conductor server and worker fleet, and reproducible even after a manual pause/resume and a forced re-decide. (The run's *root* stall turned out to be a separate bug — a `DO_WHILE` nested directly inside another `DO_WHILE`, which Conductor does not support — fixed independently in `coding-harness-openspec-planning`; re-running the same scenario against SQLite after that fix completed cleanly with zero database errors.) The SQLite errors themselves were real and independently reproducible, though: SQLite fundamentally serializes writes to one connection at a time, and Conductor OSS's own shipped config (WAL mode + a 15s busy timeout) mitigates simple lock waits but not write-write conflicts across multiple pooled connections under a genuine concurrent-write burst (e.g. several `FORK_JOIN_DYNAMIC` branches completing within milliseconds of each other, or a workflow retried/swept repeatedly). Conductor OSS ships an officially supported Postgres-backed alternative (the `conductoross/conductor` image + a `postgres:16` container) for exactly this class of problem. Adding it as a documented, run.sh-wired opt-in gives anyone who hits it a reliable local backend without raising the setup bar for everyone else — independent of, and complementary to, the nested-`DO_WHILE` fix.

## What Changes

- Add a Docker Compose file that brings up the official `conductoross/conductor` server image (Postgres-backed persistence/queue/indexing, Elasticsearch disabled) alongside a `postgres:16` container, on the same `localhost:8080` API surface the SQLite path uses today.
- Add a `run.sh` mode (and/or a `.env`-driven backend toggle) to bring up the Postgres-backed stack instead of `conductor server start`, without changing the default: `./run.sh` (no args) keeps using the SQLite quickstart unless the operator opts in.
- Document when to reach for the Postgres backend (parallel-heavy workflows hitting `SQLITE_BUSY`) versus the SQLite default (quick/casual single-workflow runs), and the new Docker prerequisite that path introduces.
- No changes to workflow JSON, worker code, or task definitions — this is purely an operational/infrastructure option for how the local Conductor server itself is run.

## Capabilities

### New Capabilities
- `harness-postgres-backend`: an opt-in, Docker-Compose-provisioned, Postgres-backed Conductor server that `run.sh` can bring up instead of the default SQLite quickstart server, for workflows that need reliable concurrent-write persistence.

### Modified Capabilities
(none — this adds a new operational path; it does not change any existing workflow/spec-level behavior.)

## Impact

- `coding-harness/docker-compose.postgres.yml` (new) — `conductoross/conductor` server + `postgres:16`, pinned image tags, a named volume for Postgres data, and a healthcheck-gated startup order.
- `coding-harness/run.sh` — new `server-postgres` (or equivalent) mode / backend-selection branch in `ensure_server()`.
- `coding-harness/.env.example` / `.env` — new toggle (e.g. `CONDUCTOR_BACKEND=sqlite|postgres`) and any overridable Postgres connection settings.
- `coding-harness/README.md`, `coding-harness/workers/README.md`, `coding-harness/SKILL.md` — document the new opt-in path, when to use it, and the added Docker prerequisite.
- No changes to `coding-harness/workers/workflows/**`, task definitions, or Python worker code.
