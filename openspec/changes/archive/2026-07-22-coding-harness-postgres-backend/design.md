## Context

`coding-harness`'s local Conductor server today is whatever `conductor server start` (the `conductor` CLI) downloads and runs: a fat jar (`~/.conductor-cli/server/oss/latest/conductor-server.jar`) with `conductor.db.type=sqlite`, `conductor.queue.type=sqlite`, `conductor.indexing.type=sqlite` baked into its packaged `application.properties`, using a `c123.db` SQLite file (WAL mode, 15s busy timeout) in the CLI's working directory. The `conductor` CLI's `server start` command has no flag to select a different backend (only `--port`, `--version`, `--foreground`, `--oss`/`--orkes`).

Confirmed live (this session): running `code_parallel` → `openspec_plan` — which drove a `FORK_JOIN_DYNAMIC` fan-out — against the default SQLite server produced correct behavior up through two full artifact-generation passes (proposal, then design+specs in parallel), then stalled permanently. Server logs showed repeated `com.netflix.conductor.core.exception.NonTransientException: [SQLITE_BUSY]` / `[SQLITE_BUSY_SNAPSHOT]` on the workflow's state-transition persistence, reproduced even after a manual pause/resume and a forced `/api/workflow/decide` call. SQLite allows exactly one writer at a time; WAL + busy_timeout mitigate simple lock waits but not write-write conflicts across Hikari's pooled connections (default pool size ~10) under bursty concurrent completions.

**Important correction, discovered mid-investigation**: re-running the identical scenario against a freshly stood-up Postgres-backed server (this change's own deliverable) hit the *exact same stall point* with **zero** exceptions of any kind. That ruled out the database as the cause and led to finding the real bug: the `openspec_plan` workflow (from `coding-harness-openspec-planning`) nested a `DO_WHILE` directly inside another `DO_WHILE`'s `loopOver`, which Conductor does not support — it silently stops advancing, independent of persistence backend. That bug is now fixed (extracted into a `SUB_WORKFLOW`, per the product docs' own guidance), and re-running the scenario after the fix completed cleanly on **both** SQLite and Postgres, with zero database errors on the SQLite run. So: the SQLite errors captured above are real and independently reproducible artifacts of SQLite's write concurrency limits, but they were not, in the end, the thing blocking that specific run — the fix that actually unblocked it lives in the other change. This change remains worthwhile as a documented mitigation for genuine SQLite write-lock contention (confirmed to occur under a 2-way parallel fan-out plus repeated sweep/retry pressure), just not framed as "the fix" for a specific stall it turned out not to cause.

Conductor OSS ships an officially supported alternative for this: a published `conductoross/conductor` Docker image plus a reference `docker-compose-postgres.yaml` / `config-postgres.properties` pairing it with a `postgres:16` container (`conductor.db.type=postgres`, `conductor.queue.type=postgres`, `conductor.indexing.type=postgres`, Elasticsearch disabled via `conductor.elasticsearch.version=0`).

## Goals / Non-Goals

**Goals:**
- Give the harness a reliable local backend option for parallel-heavy workflows (`code_parallel`, `openspec_plan`) that reproducibly hits SQLite write contention.
- Keep it strictly opt-in: `./run.sh`'s default behavior (SQLite quickstart, no Docker required) is unchanged.
- Reuse Conductor OSS's own officially published image/config rather than building or vendoring a custom server image.

**Non-Goals:**
- Not changing any workflow JSON, task definition, or worker code — this is purely how the local Conductor *server* is run.
- Not standing up Elasticsearch, Redis, or any other component beyond Postgres — indexing rides on Postgres, matching the upstream reference config.
- Not covering production/hosted deployment guidance beyond a pointer; this is a local-dev/test opt-in.

## Decisions

### D1: Docker Compose with the official `conductoross/conductor` image, not a source build
Conductor OSS's own `docker-compose-postgres.yaml` builds the server from a `Dockerfile` in the full monorepo checkout. Instead, `coding-harness/docker-compose.postgres.yml` references the published `conductoross/conductor` image directly (pinned to a specific tag, not `latest`), paired with a `postgres:16` container — mirroring the upstream reference config's properties (`conductor.db.type=postgres`, `conductor.queue.type=postgres`, `conductor.indexing.type=postgres`, `conductor.elasticsearch.version=0`) via environment variables or a mounted properties file, whichever the image's entrypoint supports for property overrides.

*Alternative considered*: vendor/clone the upstream `docker-compose-postgres.yaml` + build context. Rejected — it requires checking out the full `conductor-oss/conductor` monorepo and building from source, a much heavier and slower dependency than pulling a pinned, published image.

### D2: Backend selection lives in `run.sh`, gated by an explicit opt-in var
`run.sh`'s `ensure_server()` branches on a new `CONDUCTOR_BACKEND` env var (read from `.env` like every other operator knob): unset or `sqlite` (default) keeps calling `conductor server start`; `postgres` runs `docker compose -f docker-compose.postgres.yml up -d --wait` instead. Both paths still finish by confirming `conductor workflow list` succeeds against `CONDUCTOR_SERVER_URL`, so the rest of `run.sh` (register, worker startup) doesn't need to know which backend is running.

*Alternative considered*: a separate `./run.sh server-postgres` subcommand instead of an env toggle. Rejected — an env var composes with the existing `.env`-driven config model and lets `./run.sh run` "just work" once a user opts in once, rather than requiring them to remember a different subcommand every time.

### D3: Postgres credentials/ports are overridable but default to the upstream reference values
Default to the same `conductor`/`conductor`/`postgres` username/password/db the upstream reference config uses (this is a disposable local dev container, not a shared credential), with `.env` overrides available (`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT`) for anyone who wants to point at a differently-configured or pre-existing Postgres instance instead of the bundled container.

## Risks / Trade-offs

- **[Risk] Docker becomes a new prerequisite for this path.** → Mitigation: strictly opt-in (D2); the default zero-Docker quickstart is untouched, and docs are explicit about when Docker is actually needed.
- **[Risk] Pinned image tag can drift stale relative to the SQLite path's CLI-downloaded `--version latest`.** → Mitigation: document the pinned tag in `.env.example` and how to bump it; not auto-tracking "latest" is a deliberate reproducibility choice.
- **[Risk] Local Postgres container data (`pgdata-conductor` volume) can accumulate across unrelated test runs.** → Mitigation: document `docker compose -f docker-compose.postgres.yml down -v` to reset it; this mirrors how a stale local SQLite `c123.db` is already something operators clean up manually today.
- **[Risk, observed] A leftover SQLite `conductor server start` process can still be bound to port 8080 when switching to the Postgres backend, silently shadowing it** (confirmed live: `conductor workflow list` kept answering from the old process even after the Postgres container reported healthy). → Mitigation: document `conductor server stop` (or killing the process) before switching backends; `ensure_server()` already only runs when `conductor workflow list` first fails, but that check doesn't distinguish *which* backend is answering.
- **[Risk, observed] Local Docker credential-helper misconfiguration can block image pulls even for public images** (hit locally: Docker's `credsStore` pointed at `docker-credential-osxkeychain`, which some Docker Desktop alternatives — e.g. Rancher Desktop — don't put on `PATH` by default, so `docker compose up` fails with "executable file not found" before it ever reaches the registry). → Mitigation: this is host Docker configuration, not something `docker-compose.postgres.yml` can fix; call it out in the docs as a thing to check if the pull step fails immediately.

## Migration Plan

1. Add `coding-harness/docker-compose.postgres.yml`.
2. Add `CONDUCTOR_BACKEND` (and Postgres connection overrides) to `.env.example`, defaulting to unset (`sqlite` behavior).
3. Update `run.sh`'s `ensure_server()` to branch on `CONDUCTOR_BACKEND`.
4. Update `README.md` / `workers/README.md` / `SKILL.md` prerequisites with the opt-in path and when to use it.
5. No data migration — this only affects a *local* Conductor server's own persistence, not harness workflow data or the target repos it operates on.

## Open Questions

- Does the `conductoross/conductor` image accept a mounted `application.properties`/`CONFIG_PROP`-style override the same way the upstream monorepo's Dockerfile-built image does, or does it need the equivalent settings passed as individual `SPRING_...`/`CONDUCTOR_...` environment variables? Confirm against the actual image before finalizing `docker-compose.postgres.yml` (captured as a task in `tasks.md`).
