## 1. Verify the image's config-override mechanism

- [x] 1.1 Confirmed via the upstream `docker/server/bin/startup.sh` and Dockerfile (research), then corrected via live testing once Docker was available: the image bakes in `/app/config/config-postgres.properties` (and siblings) at build time; setting the `CONFIG_PROP=config-postgres.properties` env var makes `startup.sh` launch java with `-DCONDUCTOR_CONFIG_FILE=/app/config/$CONFIG_PROP`, which sets `conductor.db.type=postgres`, `conductor.queue.type=postgres`, `conductor.indexing.type=postgres`, `conductor.elasticsearch.version=0`. **Correction (research was wrong here, live testing caught it)**: `SPRING_DATASOURCE_URL`/`_USERNAME`/`_PASSWORD` env vars do **not** override the values baked into that file — `CONDUCTOR_CONFIG_FILE` loads at higher precedence than env vars. The container failed with `UnknownHostException: postgresdb` when I tried to point it at a differently-named Postgres service via env var. Fix: name the Postgres service `postgresdb` (matching the hardcoded hostname) and use its hardcoded `conductor`/`conductor`/`postgres` user/password/db — matching the baked-in defaults exactly is the only working approach with this image, not overriding them. Pinned image tag: `3.22.3` (latest non-RC on Docker Hub at time of writing).

## 2. Docker Compose stack

- [x] 2.1 Added `coding-harness/docker-compose.postgres.yml`: a `postgresdb` service (name fixed, not overridable — see 1.1; `postgres:16`, hardcoded `conductor`/`conductor`/`postgres` user/password/db matching the image's baked-in profile; named volume `pgdata-conductor`; healthcheck via `pg_isready`) and a `conductor-server` service (pinned `conductoross/conductor:3.22.3`, `CONFIG_PROP=config-postgres.properties`, port `8080` published, `depends_on: postgresdb: condition: service_healthy`, a healthcheck against `http://localhost:8080/health`). Validated with `docker compose config`, then live.
- [x] 2.2 Verified live once Docker was available: `docker compose -f docker-compose.postgres.yml up -d --wait` brought up both containers healthy; `conductor workflow list` against `http://localhost:8080/api` returned a clean server (only the image's built-in `kitchensink`/`sub_flow_1` samples — confirming it was genuinely the new Postgres-backed server, not a stale process still holding port 8080, which did happen once and had to be killed first). `./workers/register.sh` then registered all task defs and workflows against it successfully.

## 3. run.sh wiring

- [x] 3.1 Added a `CONDUCTOR_BACKEND` branch to `run.sh`'s `ensure_server()`: unset/`sqlite` keeps calling `conductor server start` (unchanged); `postgres` calls `docker compose -f docker-compose.postgres.yml up -d --wait` instead. Both branches fall through to the existing `conductor workflow list` readiness check. Verified with `bash -n run.sh`.
- [x] 3.2 The `require docker` guard sits inside the `postgres` branch only (mirroring `require conductor`/`require jq`), so the default SQLite path never needs Docker.

## 4. Config surface

- [x] 4.1 Added `CONDUCTOR_BACKEND`, `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`/`POSTGRES_PORT`, and `CONDUCTOR_IMAGE_TAG` to `coding-harness/.env.example`, all commented out (default unset → `sqlite` behavior unchanged). Also fixed the pre-existing drift in `.env.example`'s `WORKER_MODULES` default (was still `coding_agent,gitops`, missing `openspecops` from the prior change).

## 5. Documentation

- [x] 5.1 Updated `coding-harness/README.md` (Prerequisites) and `coding-harness/workers/README.md` (Prerequisites) with the opt-in Postgres path: when to use it, the `CONDUCTOR_BACKEND=postgres` toggle, and Docker as that path's added prerequisite.
- [x] 5.2 Updated `coding-harness/SKILL.md`'s gotchas section with the same pointer (recognize `NonTransientException: [SQLITE_BUSY...]` in logs → suggest the Postgres backend), so an agent operating the harness knows what to do when it sees a SQLite busy/lock error.

## 6. Verification

- [x] 6.1 Re-ran the `code_parallel` → `openspec_plan` live scenario against the Postgres-backed server. **First attempt hit the identical stall point with zero exceptions** (not `SQLITE_BUSY` — nothing at all), which is what led to discovering the real cause was a nested-`DO_WHILE` bug in `openspec_plan.json` (fixed in `coding-harness-openspec-planning`; see that change's design.md D2/Risks and tasks.md 3.4). **After that fix**, re-ran end-to-end on the Postgres backend: proposal → design+specs (parallel) → tasks.md → AI-judge approval → `openspec_tasks_to_subtasks` → coding fan-out → clean merge, zero conflicts, correct code (`subtract(a, b)` added, `add` unchanged, verified in the target repo's git history). Also confirmed the same scenario completes cleanly on the plain SQLite backend post-fix, with zero database errors — establishing that the Postgres backend, while independently valuable for real SQLite write-contention scenarios, was not what unblocked this specific run.
- [x] 6.2 Confirmed by inspection: the `run.sh` edit only wraps the pre-existing `conductor server start` call in an `if [ "${CONDUCTOR_BACKEND:-sqlite}" = "postgres" ]` / `else` — the default (unset) branch's command is byte-for-byte unchanged, `require docker` is unreachable from it, and `bash -n run.sh` passes.
