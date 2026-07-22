## Purpose

An opt-in, Docker-Compose-provisioned, Postgres-backed Conductor server that `coding-harness`'s `run.sh` can bring up instead of the default SQLite quickstart, for operators who hit SQLite write-lock contention (`NonTransientException: [SQLITE_BUSY...]`) under concurrent workflow writes. Strictly additive — the default zero-Docker SQLite path is unchanged.

## Requirements

### Requirement: Default local server path is unchanged
`./run.sh` (and `./run.sh run`) with no backend configured SHALL continue to bring up the existing SQLite-backed `conductor server start` quickstart exactly as before this change.

#### Scenario: no backend opt-in configured
- **WHEN** `./run.sh` runs with no `CONDUCTOR_BACKEND` set (and no prior opt-in)
- **THEN** it starts the local server via `conductor server start` (SQLite), unchanged from today

### Requirement: Opt-in Postgres-backed server
The harness SHALL provide a Docker-Compose-provisioned, Postgres-backed Conductor server, selectable via an explicit opt-in, as an alternative to the SQLite quickstart.

#### Scenario: operator opts into the Postgres backend
- **WHEN** the operator sets `CONDUCTOR_BACKEND=postgres` (in `.env` or the environment) and runs `./run.sh`
- **THEN** `run.sh` brings up `coding-harness/docker-compose.postgres.yml` (the `conductoross/conductor` server image + a `postgres:16` container) instead of calling `conductor server start`
- **AND** the server is reachable at the same `CONDUCTOR_SERVER_URL` (`http://localhost:8080/api` by default) the SQLite path uses

#### Scenario: Postgres container must be healthy before the server starts
- **WHEN** the Postgres-backed stack is brought up
- **THEN** the Conductor server container does not start accepting traffic until the `postgres:16` container reports healthy
- **AND** Postgres data persists across restarts via a named Docker volume

#### Scenario: indexing does not require a separate Elasticsearch
- **WHEN** the Postgres-backed server starts
- **THEN** it uses Postgres for indexing (`conductor.indexing.type=postgres`) with Elasticsearch connectivity disabled (`conductor.elasticsearch.version=0`), requiring no additional search service

### Requirement: Documented guidance on when to use which backend
The harness's documentation SHALL explain when to reach for the Postgres backend versus the SQLite default, and the added Docker prerequisite.

#### Scenario: parallel-heavy workflow hits SQLite write contention
- **WHEN** a user reads the harness docs after `code_parallel`/`openspec_plan` fails with a SQLite busy/lock error
- **THEN** the docs point them at the Postgres-backed opt-in path and its `CONDUCTOR_BACKEND=postgres` toggle, with Docker listed as its prerequisite
