# Scale & Enterprise Review — 2026-11-07

Review of the coding harness against three targets set by the owner:

1. **Power:** able to run a program the size of "rewrite the Linux kernel".
2. **Scale:** manage thousands of concurrently running agents, rolled out across a large enterprise.
3. **Experience:** the TUI and the web UI are how humans launch, watch, steer and stop that work.

Every claim below is tied to a file and line in this repository or to something observed on this
host today. Section 6 is the plan; section 7 is what was fixed during the review.

## 1. Verdict

The construction model is right and proven: a durable Conductor team cycle (standup → fan-out →
implement ∥ review → integrate → learn → re-plan), frozen contracts with acceptance tests, peer
review with a hidden oracle, monitors, retrospectives into a learning store, and passive humans who
steer by plain message. That is the part that is hard to get right, and it is live-proven
(`docs/closure_result.md`). The long-run proof is done too: on 2026-11-08 the thirteenth
`cc_program` run on resp-cache (`688f426b`, $32.98, 1 h 58 min) delivered all six subsystems — four
design documents, 22 code items across five teams, 154 frozen acceptance tests authored by separate
agents before implementation and all green at the delivered head, `go vet` and `go test -race`
green, every item peer-reviewed, no blocked or quarantined item, zero task failures — after twelve
earlier runs each died on a different harness defect that was found live and fixed the same day
(section 7, twenty-six of them).

Everything around it is a **single-host, single-tenant prototype**:

- one host, one shared filesystem, 8–16 agent sessions, a SQLite Conductor;
- planning that must emit a whole program as one JSON reply from a 400-file tree primer;
- a console that lists raw run IDs, cannot launch, cannot page, cannot alert, and a TUI that reads
  the blackboard directly;
- no identity beyond a token file, every secret visible to every agent sandbox, no metrics, no
  packaging.

None of these is a design flaw in the team cycle. They are the substrate, the planner, the operator
surface and the enterprise envelope, and each is a bounded piece of work (section 6).

## 2. Found live during this review

These were not reported by anyone; the review found them by looking at the running system.

| Finding | Mechanism | Effect | Status |
|---|---|---|---|
| **Agent poller parked forever** | `coding_agent` poller (pid 60595) blocked in `recv` on one batch poll the server never answered. conductor-python 1.3.11 passes `timeout=None` to httpx (`rest.py:198-204`), which *disables* the client's 120 s default. | Every `coding_agent` task sat SCHEDULED; the new resp-cache run could not start; nothing alerted for 69 minutes. | **Fixed** (§7): poll response bound = long-poll timeout + 30 s, then poll again. |
| **Verification fleet absent for 6.5 h** | `verification` is a separate deployment by design (`main.py:7-9`) but nothing supervises or health-checks it. | `runtime_health` 42c3913c waited 394 min on `verification_health`; any `test_run` would have waited too. | Restarted by hand. Needs a fleet-liveness check (§6, S1). |
| **Pollers are silent** | Task-runner children log nothing after startup; `workers-main.log` mtime equals fleet start time while a $21 run executed hundreds of tasks. | No per-task log line, no error trail, nothing to grep when a poller dies. | §6, S1 (structured logs + metrics). |
| **Queue age is invisible** | No check compares `scheduledTime` of SCHEDULED tasks to the last poll of their queue (`/api/tasks/queue/polldata/all` has the data). | Both stalls above were discovered by a human reading JSON. | §6, S1 (fleet health task + console fleet view). |

## 3. Ceilings today

| Dimension | Today | Where it is set | What it means for 1,000 agents |
|---|---|---|---|
| Agent sessions per host | 8 default, 16 max | `coding_agent/tasks.py:506`, `common/polling.py` `min(16, …)` | 63+ hosts just for the cap; the cap is not derived from host memory. |
| Memory per session | 750–880 MB RSS observed per `claude` CLI | — | 16 sessions ≈ 13 GB; a 128 GB host could run ~120 sessions if the cap were capacity-based. |
| Idle poller footprint | 127 processes × ~33 MB ≈ 4.2 GB per host (one OS process per task type) | SDK `task_handler.py`; `main.py:41` default modules | Agent hosts must run `WORKER_MODULES=coding_agent` only (1 process); control hosts run the rest. |
| Poll load | 127 long polls / 5 s per host | `common/polling.py` | 100 hosts ≈ 2,500 polls/s on a SQLite server. |
| Conductor backend | SQLite via `conductor server start` | `run.sh:75`; jstack shows `SqliteQueueDAO` | Postgres compose exists (`docker-compose.postgres.yml`); Orkes Conductor is the enterprise answer. |
| Shared state between tasks | absolute paths on one filesystem (`worktreePath`, `sharedRepo`, `.cc-worktrees`) | `common/git.py:23,571`, `gitops/tasks.py:260,291,372`, `team/tasks.py:544,861,970` | Tasks of one run cannot spread across hosts. **The** scale-out blocker. |
| Blackboard | Postgres optional (`SWARM_BLACKBOARD_PG_DSN`), default local files; `fold_board` re-reads 6 kinds and re-folds every call | `workers/common/blackboard.py:88-241`, `common/team_board.py:342-355` | Fine to ~100 runs; the per-process LRU and whole-prefix listing become the hot path past that. |
| Wave width | `maxWidth` 6 per team, `maxWaves` 12/16, `maxDepth` 2, no cap on teams per rank | `team_build.json:73-74`, `cc_program.json:47-48`, `stages/tasks.py:254` | Fan-out is fine; the planner is the limit (next row). |
| Planner input | file tree primer 400 files / 8,000 chars; guide 24k; diff 24k; whole plan in one JSON reply with "EVERY file" listed | `common/coding_agent.py:50`, `common/templating.py:35`, `team/tasks.py:70`, planner prompt in `cc_program.json` | A kernel tree (~80k files) cannot be planned this way at all. |
| Cross-team knowledge | contracts frozen once (`stages/tasks.py:227`); children receive directives only; no change requests between teams | `team/tasks.py:159-168,316-341` | Rank N cannot consume rank N-1's delivered API as a contract, nor ask for a change. |
| Toolchains | java, go, python, ruby, typescript (dotnet/php/elixir optional); `make` restricted to test/check/build goals | `common/check_execution.py:29-67`, `common/verification.py:315,1555-1596` | No C family, no Kbuild/Kconfig, KUnit, kselftest, QEMU. |
| Resume | no program-level resume; `resumeSessionId` unwired; a retry re-plans from scratch and skips BLOCKED subsystems as "built" | `stages/tasks.py:251` | A months-long program cannot survive a fleet rebuild cleanly. |
| Identity | token file `~/.conductor-code/actors.json`; TUI without a token writes as `human`; no run owner recorded | `workers/common/actors.py`, `tui/board.py:30-35`, `tui/api.py:648-653` | Any credential holder can terminate any run. |
| Secrets | worker env (model keys, `GH_TOKEN`, Conductor secret, PG DSN) inherited into every agent sandbox; stderr/turn log returned unredacted | `common/coding_agent.py:596-599`, `check_execution.py:158-164`, `codex.py:341`, `gemini.py:117`, `coding_agent/tasks.py:706` | Not deployable to a shared enterprise fleet as is. |
| Alerting | desktop notification only; nothing pushes an alert when a run needs attention | `tui/notify.py`, `tui/console.py:212-222` | Nobody is paged when 1,000 agents stall or overspend. |
| Packaging | bash supervisor over a local venv; no image, Helm, systemd/launchd | `run_workers.sh` | No way to run 60 hosts reproducibly. |

## 4. Gap analysis by target

### 4.1 "Rewrite the Linux kernel"

The kernel is ~80k files, ~35M lines, hundreds of subsystems with their own build and test
conventions, and a rewrite is hundreds of teams over months. Against that:

- **Planning is one-shot and flat.** One read-only agent must return every subsystem with every
  file in a single reply, after seeing at most 400 paths. Recursion exists (`stages/tasks.py:253-278`)
  but the planner is never asked to nest, and `maxDepth` is 2.
- **Agents navigate by a truncated primer, not an index.** There is no symbol map, no "who owns
  this directory", no way to ask "where is `struct file_operations` implemented" except Grep over
  the tree inside each session.
- **Contracts do not accumulate.** A delivered subsystem's public interface never becomes the
  frozen contract for the ranks that depend on it, and a dependent team cannot file a change
  request against it; its only move is pushback.
- **No C.** The verification contract has no C family, `make` is confined to test-shaped goals, and
  Kbuild/KUnit/kselftest/QEMU are absent. A kernel program would end `command_discovery_blocked`
  on its first item.
- **No long-horizon resume.** Budget halts drain and stop (correct), but there is no "continue
  this program from its board" and a BLOCKED subsystem counts as built on retry.
- **Budget is flat.** Each child team gets an equal share of the remaining ceiling regardless of
  size (`stages/tasks.py:255-257`); `cc_program` itself has no budget gate.

### 4.2 Thousands of concurrent agents

- **Filesystem coupling** (table row "Shared state") is the hard blocker: coding, gitops, team and
  verification tasks of one run exchange absolute paths and serialize on an `flock` in the git dir
  (`common/git.py:102-114`). Horizontal scale requires that the git remote, not a path, be the only
  shared state.
- **Capacity is a constant, not a measurement.** 16 sessions per host regardless of RAM; no
  admission control; duplicate-delivery protection is per process (`coding_agent/tasks.py:94-125`).
- **Fleet partitioning does not exist.** No Conductor task domains (0 of 127 workers set one), so a
  tenant, a team or a host class cannot be given its own pool.
- **Control plane:** SQLite Conductor; process-per-task-type pollers; lost poll responses were
  fatal until today; no metrics (`TaskHandler` gets no `metrics_settings`); no structured logs.
- **Blackboard folds** are whole-log and per process; `_swarm_learnings` is a single global run
  that every reader lists.

### 4.3 TUI and web UI

What exists: a Textual TUI (dashboard of 50 runs, run detail, team board with nudge/stop/reopen/
terminate/message, chat with tools, launcher forms) and a stdlib web **Swarm Console** (run picker,
message box, "Now", cost burn, escalations, kanban, teams, trace, lessons; `POST /api/steer`,
`/api/ask`). Live state on demand (`/api/state`) and plain-message steering work and are proven.

What an operator of 1,000 agents does not get:

- **No fleet view.** Nothing shows hosts, pollers, queue depth per task type, stuck SCHEDULED
  tasks, spend per minute, or runs across projects. Today's two stalls were invisible in both UIs.
- **Run lists are raw.** `/api/runs` returns bare IDs (including `_swarm_learnings`); the TUI caps
  at 50 with no paging; neither shows owner, description, started-at or spend in the list.
- **Two data paths.** The TUI reads the blackboard and Conductor directly (`tui/board.py:47-67`,
  `tui/swarm_state.py:43-53`); the console has its own JSON. Two clients, two models, no SSE.
- **Cannot launch from the web; `team_build` cannot be launched anywhere** (`tui/catalog.py:754`).
- **No per-agent view or kill.** Agents are a name, last tool and seconds-ago; no cost, elapsed,
  file being edited, or transcript tail; "stop" is a blocking directive on the item, not a kill.
- **No alerts.** Escalations, budget warn/halt and run end push nothing beyond a desktop bell.
- **Learning board is read-only** in both UIs; retracting a wrong learning needs Python.

### 4.4 Enterprise rollout

- No tenant/org/project model; no RBAC; any actor can steer or terminate any run; console GETs are
  unauthenticated (`tui/console.py:29`); `ownerEmail` is one person in 35 workflows and 116 taskdefs.
- Secrets inherited into sandboxes and echoed into task output; Codex write mode and Gemini have
  network access (`codex.py:148`, `gemini.py:20-23`).
- `policy.defaults.json` declares protected paths, secret patterns and destructive commands and
  **nothing reads it**; no secret scanning, license check or CODEOWNERS awareness.
- Model policy can be overridden by the repository and by the caller
  (`model_policy.py:155-196`); no org allowlist or pin; no Bedrock/Vertex routing.
- No image, chart, service unit, backup, HA or runbook docs.

## 5. Target architecture

What stays: Conductor as the durable control plane; the team cycle, contracts, review, monitors,
retrospective and blackboard semantics; the Claude Agent SDK sandbox; passive humans.

What changes:

```
                 ┌─────────────── operators (SSO) ───────────────┐
                 │   Web app  ──┐                 ┌── TUI          │
                 └──────────────┼── one State API ┼───────────────┘
                                │  (/api/fleet, /api/runs, /api/state, SSE, /api/steer)
┌───────────────────────────────┴──────────────────────────────────────────┐
│  Conductor (Orkes, or OSS+Postgres)  —  domains per org/pool, RBAC       │
└───────┬───────────────────────────┬───────────────────────┬──────────────┘
   control fleet               agent fleet (N hosts)     verify fleet
   (team/stages/gitops,        WORKER_MODULES=coding_agent  WORKER_MODULES=verification
    1 host class)              capacity = f(host RAM)       ephemeral clones
        │                           │                           │
        └──── git remote (only shared state) ──── Postgres blackboard ────┘
                     + symbol index per run      + per-org learnings
```

- **Git remote is the only shared state.** A task receives `(repoUrl, branch, head)` and makes an
  ephemeral host-local worktree; item branches are pushed on every commit. `.cc-worktrees` becomes
  a per-host cache. The continuous-rebase integrator keeps working; it fetches instead of reading a
  sibling's path.
- **Three fleet roles, partitioned by Conductor domains**, each a container image with a Helm
  chart and queue-depth autoscaling. Agent hosts run one poller process with a capacity derived
  from memory.
- **One State API** backs both UIs: fleet, runs (paged, searchable, with owner/spend/status),
  per-run state, per-agent detail and transcript tail, SSE for live updates, steer and launch.
- **Recursive planning over a repo index.** Programs plan by directory ownership to depth N;
  agents query a symbol index tool instead of a 400-file primer; delivered interfaces become the
  next rank's contract; teams can file change requests on each other's boards.
- **Org envelope.** Identity from SSO, run ownership, RBAC, per-org learnings and budgets,
  secrets never inherited, policy file enforced.

## 6. Plan

Each phase ends in a live Conductor proof, as the closure plan did. Order: **S1 → S3 → S2 → S4**,
with the secret-isolation item of S4 pulled into S1 because it is cheap and blocks any shared fleet.

### S1 — Host-independent swarm (unblocks every other target)

1. Replace path hand-off with `(repoUrl, branch, head)` in `workspace_prepare`, `worktree_add`,
   `code_subtask`, `team_*`, `test_run`; push item branches on commit; ephemeral worktrees with a
   per-host cache. Remove the cross-task `flock`.
2. Postgres blackboard mandatory for swarm workflows; a per-run materialized board row updated by
   the writer so `fold_board` is one read. `_swarm_learnings` keyed per org.
3. Fleet roles via `WORKER_MODULES` + Conductor `domain` per pool; `coding_agent` capacity from
   host memory (sessions = free RAM / measured session RSS, overridable), replacing the 16 cap.
4. Fleet health: a `fleet_health` task that compares SCHEDULED task age with queue poll data and
   verifies every required role is polling; exposed at `/api/fleet`; alert on breach.
5. Structured JSON logs per task (run, team, item, agent ids) and SDK metrics enabled
   (`metrics_settings`), scraped by Prometheus/OTel.
6. Secrets: agent env allowlist (never `os.environ`), redaction of task output, keys via secret
   manager env on the agent fleet only.
7. Packaging: worker image, Helm chart (control/agent/verify deployments, KEDA on queue depth),
   `register.sh` as a job.

**Proof:** one `team_build` whose coding, gitops and verification tasks run on three different
hosts (containers) sharing nothing but a git remote and Postgres; 100 concurrent agent sessions
across two agent hosts; a killed poller is reported by `fleet_health` within one poll interval.

### S3 — Operating thousands of agents (TUI + web on one API)

1. State API: `/api/fleet`, `/api/runs?status=&owner=&project=&q=&page=`, `/api/state`,
   `/api/agents/{id}` (cost, elapsed, current file, last 50 transcript lines), SSE stream,
   `/api/launch`, `/api/steer`, `/api/learnings` (retract/add). TUI becomes a client of it.
2. Web app: fleet page (hosts, pools, queue depth, stuck tasks, spend/min), runs page (paged, named,
   owner, spend, status), run page (today's board + per-agent drill-down + transcript tail), launch
   forms from the catalog, learning board editor. Replace the inline HTML with a static front-end
   served by the same Python server.
3. Per-agent kill: a `kill` control fact consumed by the feedback hook ends that session; the item
   is re-queued.
4. Alerts: webhook/Slack/email on escalation, budget warn/halt, run end, fleet health breach.
5. Run identity: who launched, description, project, started at, stored with the run.

**Proof:** 50 concurrent runs visible, filterable and pageable; one agent killed from the web with
its item re-dispatched; a Slack alert on a forced budget halt.

### S2 — Kernel-scale construction

1. Recursive planner by ownership: each level plans its scope from a scoped tree, emits
   directories/globs per subsystem, nests `subsystems` to depth N (config, budget-bounded).
2. Repo index: symbols/files/owners per run (tree-sitter/ctags into Postgres) exposed to agents as
   an MCP tool; replaces the 400-file primer for repos above a threshold.
3. Living contracts: `program_apply_rank` adds a delivered subsystem's declared public interface to
   `contractPaths` for dependents; a `request` fact lets a team ask another team for a change,
   scheduled by the target's next standup.
4. Program resume: `cc_program` input `resumeRun` rebuilds the rank state from the board; BLOCKED
   subsystems are re-planned, DONE ones skipped; `resumeSessionId` wired for agents.
5. C family: gcc/clang/make/cmake in runtime health; `make -C <dir> <target>` allowed as a targeted
   command; KUnit, kselftest and a QEMU boot smoke documented as discoverable commands; C in
   `_LANG_MARKERS`.
6. Budget by size: child ceilings proportional to planned item count; `cc_program` budget gate.

**Proof:** a program over a ≥10k-file C repository (a kernel subtree) planned to depth 3, built
rank by rank with KUnit as the acceptance oracle, resumed after a forced fleet rebuild.

### S4 — Enterprise envelope

1. SSO (OIDC) → actor identity; run ownership; RBAC (view / steer / terminate / launch / admin)
   per project; console GETs authenticated.
2. Tenancy: org/project prefix on blackboard ids and Conductor domains; learnings, budgets and
   model policy per org; daily/monthly cost ceilings.
3. Policy enforcement: protected paths, secret scanning and destructive-command rules from
   `policy.defaults.json` applied at commit and integrate; CODEOWNERS-aware reviewer selection.
4. Model governance: org allowlist and pin; Bedrock/Vertex routing; repository overrides disabled
   by default.
5. Audit export (facts + commits + PRs per run), backups, HA and runbook docs.

**Proof:** two orgs on one deployment cannot see, steer or learn from each other's runs; a
protected-path edit is rejected at integrate; a daily org ceiling halts new dispatch.

## 7. Fixed during this review

- **Lost poll responses no longer park a poller.** `common/polling.py` bounds the wait for a
  batch-poll response to the server's long-poll timeout plus 30 s and lets the SDK poll again;
  task execution, lease renewal and result updates carry no bound. Regression tests in
  `tests/test_execution_runtime.py`; policy note in `AGENTS.md` (Timeout Policy); `workers/README.md`.
- **Verification fleet restarted** so `test_run`, `test_discover` and `verification_health` are
  polled again; `runtime_health` 42c3913c completed with both health flags true.
- A fresh resp-cache program (`fc6467b7`) was started on the fixed harness with the previous
  run's input to prove the long run end to end. It ended `PROGRAM_INCOMPLETE` at 2/6 subsystems
  ($14.83): `st-lru`'s acceptance criteria needed eviction hooks in `store/store.go`, owned by the
  sibling item `st-core`; the worktree guard refused every attempt there, the reviewer (who never
  saw the write roots) kept demanding exactly that edit, and the implementer burned both review
  rounds on work-arounds until the item was quarantined and the four downstream subsystems never
  left backlog. Three harness defects, all fixed:
  - **The Lead now widens an item's write scope from the recorded refusal.** `coding_agent` emits
    the refused in-worktree paths as `deniedWrites`; `code_subtask` carries them; `team_integrate`
    records them on the item's `session` fact; `team_standup` adds each to the item's files once no
    open item owns it (a resolved `write_scope` notice), holds the item while one does, and never
    widens a frozen-contract path. The reviewer prompt states what the implementer may write and
    asks for a named file instead of a repeated demand; the implementer prompt says to attempt the
    edit once rather than work around it. Tests in `test_team_cycle.py`, `test_coding_agent_policy.py`.
  - **Implementers can run the repository's verification command.** `CODE_SUBTASK_TOOLS` was a
    narrow subset that lacked `make`, `gofmt`, `cat` and `pytest`; it is now the full unattended
    coding surface (`DEFAULT_ALLOWED_TOOLS`). Verified against the real `claude` permission layer:
    `gofmt -l .` was denied under the old list and allowed under the new one.
  - **The learning board is visible.** The console's `_swarm_learnings` run folded team `lesson`
    facts and showed nothing; it now lists every retrospective learning (`retrospective.everything`),
    and `/api/state` carries the run's own `retrospective` summary and learnings.
- The next proof run (`a062184f`, same input, $17.10) exercised the scope widening live — the store
  team's Lead widened five items to the files their implementers were refused and the store
  subsystem delivered 7/7 — and ended `PROGRAM_INCOMPLETE` at 2/6 on five further defects, all fixed:
  - **A reviewer session reset the shared integration worktree.** `team_review_build` ran each
    reviewer in the run's shared worktree; the agent runner's end-of-session reconcile (which
    converts an agent commit back to uncommitted changes) saw the resp team's fast-forward land
    during the store reviewer's session, treated it as the reviewer's commit and reset the shared
    branch to the session's starting HEAD — resp's files became untracked and its next
    `merge --ff-only` aborted ("untracked working tree files would be overwritten"), failing the team.
    Reviewers now read their own worktree pinned at the reviewed head (`git.worktree_add_detached`,
    `.cc-worktrees/review-<item>`), removed by `team_apply_reviews`. Test in `test_team_cycle.py`.
  - **Several items refused the same file were widened together.** The standup's in-memory item map
    was not updated after a widening, so four store items were all given `store/store.go` in one
    standup and edited it concurrently (six `duplication` alerts, three extra review rounds). The
    first widened item now owns the file for the rest of the standup; the others wait for it to land.
  - **The retrospective recorded a placeholder.** The agent's StructuredOutput payloads put the
    `learnings` array inside the `summary` string as tagged text; the validator rejected eight of
    nine, and the agent sent `{"summary": "test", "learnings": [{"text": "a"}]}` to get past it —
    which was recorded on the live learning board. `coding_agent` now carries the whole payloads
    (`structuredAttempts`), `retro_record` untangles and records the first substantive candidate,
    a learning under 10 characters is never recorded, the prompt bounds the summary to three
    sentences, and the placeholder was retracted from the board.
  - **The retrospective agent read the wrong tree.** Its worktree was the source checkout (still on
    `main`), so it saw "only the scaffold". `run_retrospective` takes `workspacePath` (the run's
    branch), and `retro_evidence` reads it back from the run's `workspace` task for a failure
    workflow. Tests in `test_retrospective.py`.
  - **Subsystems were sliced before the design existed — twice the design then forbade the slice.**
    The program planner writes every subsystem's items up front; the design team's documents land in
    rank 0 and contradicted them in both runs (`fc6467b7`: st-lru's hooks in a file another item
    owned; `a062184f`: an `r-value` item creating the `resp/value.go` that `resp.md` §R.2 forbids,
    which the reviewer then rejected). Now, when a rank delivers design documents,
    `program_apply_rank` asks for a work breakdown from them, a read-only `rank_breakdown` agent
    re-derives the remaining subsystems' items in the run's workspace, and `program_rank_build`
    folds the breakdown in once per planner session (program fact, board, budget, a resolved
    `breakdown` notice). Test in `test_program_teams.py`; `docs/workflow-inputs.md` updated.
  - **A rejected structured output failed the whole session.** The next run (`a33d7061`) hit the
    same mangling in the program planner: five StructuredOutput payloads rejected (the plan as
    tagged text inside `summary`; raw input the CLI could not parse) and the planning task failed
    after $1.13 although the plan was written. The agent runner now salvages the newest submitted
    payload that satisfies the schema (`common/structured_output.py`: untangle tagged properties,
    parse `__unparsedToolInput`, validate with `jsonschema`) and returns it as the session's
    structured output (`structuredSalvaged`), for every schema-bound task — planner, reviewers,
    implementer reports, retrospective. Tests in `test_structured_output.py`, `test_coding_agent_policy.py`.
- The next proof run (`a33d7061`, same input, $17.15) proved the previous round's fixes live: the
  breakdown re-sliced all five remaining subsystems from the design documents before rank 1, every
  reviewer ran in its own `review-<item>` worktree, no `duplication` alert fired, and the program
  retrospective recorded four substantive learnings. Design and resp delivered; the store team
  delivered 6/7 and the program ended `PROGRAM_INCOMPLETE` at 2/6 on `st-lists`, blocked by a
  rebase conflict in the one file it owned. Four defects, all fixed:
  - **A fork's test-cycle repair agent wrote sibling items' files.** `test_cycle` received the item's
    `allowedWriteRoots` and never handed them to `test_discover`, so the repair agent's scope was the
    discovered directory (`store/`). Each item's frozen acceptance test called sibling APIs (`Set`,
    `Exists`, `Type`) that were being built in the same wave, so the repair agents implemented each
    other's files to force their own test green; those commits reached the shared branch inside the
    first fork to integrate, and every other fork of the wave then conflicted on rebase (three
    retries in wave 2; in wave 3 `st-keys`' repair commit added `store/lists.go`, and `st-lists`'
    own file collided with it at the attempt cap). Both `test_discover` calls now receive the declared
    write roots, and the repair agent runs under exactly the item's files. Test in `test_delivery_repair.py`.
  - **Acceptance tests depended on siblings built concurrently.** Each author is now told which
    items are its transitive dependencies (built first, may be relied on) and which are built
    concurrently or later (their files do not exist when the tests first run) and to cover only what
    the item can satisfy alone. Test in `test_team_cycle.py`.
  - **Every item with a frozen acceptance test was audited `incomplete_delivery`.** The acceptance
    test sits inside `allowedWriteRoots` only so a wrong assertion can be amended, and
    `code_subtask` used the write roots as the audit's planned paths — so an untouched oracle was a
    "missing" delivery: three no-op repair agents per fork, 15 of 16 implementation forks, roughly
    half the run's spend. `code_subtask` now carries `acceptanceTests` and both audits and the outcome
    treat them as `optionalPaths`. Tests in `test_delivery_repair.py`, `test_team_cycle.py`.
  - **A documents team was widened into code.** The design team's `d-store` was refused
    `store/errors.go` and `store/store.go` and the Lead widened it to them, giving a documents team
    ownership of files the breakdown then assigned to the store team. Widening now stops at the
    directories the team's items span; a refused path is a resolved `write_scope` notice
    (`scopeRefusals`). Test in `test_team_cycle.py`.
  - A rebase conflict's mechanism now names the shared-branch commit that last changed each
    conflicting file (`touched`), so the other side of a collision is visible without a git
    archaeology; the retrospective agent had blamed the wrong cause from the shared tree alone, and
    those two learnings were retracted.
- The fifth proof run (`ec0fb6dc`, same input, $35.48) got further than any before it: design, resp
  (3/3) and store (6/6) delivered, the server team delivered 8/8 with every fork's own gate green,
  no rebase conflict, no retry, no cross-item repair commit, the span-confined refusal fired exactly
  once (resp-reader asked for `docs/design/resp.md`), the breakdown re-sliced from the design
  documents, every reviewer ran in its own worktree, and both frozen-test amendments were decided by
  the peer reviewer. The program still ended `PROGRAM_INCOMPLETE` at 3/6, and the agents worked
  blind. Three defects, all fixed:
  - **A delivered subsystem was refused on a stale head.** The server reviewer approved amending
    `test_accept_server_connection.py` (the original regex lacked `(?m)`); `team_apply_reviews`
    merged the fork and re-froze the test at the merged head, but the wave's `finalHead` variable
    came from the integrate step that ran *before* that merge, so `team_outcome` diffed the re-frozen
    commit against the pre-merge head and reported "modified after they were frozen" — and the final
    test cycle verified that pre-merge commit, not the delivered one. cmd and integration never ran.
    `team_apply_reviews` now outputs `finalHead` (the head after its merges) and the wave reads it from
    there. Test in `test_team_cycle.py` reproduces the false verdict at the stale head and the
    delivered verdict at the right one.
  - **No agent could run a command.** 591 Bash calls across 71 sessions failed at spawn with E2BIG:
    Claude Code's sandbox adds deny paths for every worktree registered on the repository (171 of the
    profile's 199 paths), and the harness never removed a fork worktree — 70 were registered, 51 of
    them from runs finished hours earlier. Every implementer wrote its files without building or
    testing them, every delivery-repair agent spent its turns explaining that Bash was broken, and the
    retrospective recorded the symptom as an environment learning. Fork worktrees are now removed at
    the end of the wave that decided them (integrated, re-queued, discarded; a fork held for an
    amendment waits for the verdict), and `workspace_prepare` prunes the worktrees of runs the server
    reports finished before creating a new run's. Tests in `test_team_cycle.py`, `test_workspace.py`.
    What remains is external and exact: Claude Code 2.1.319 adds about three deny paths per
    registered worktree and macOS caps the exec argument list at 1 MiB, so one repository supports
    roughly 55 concurrently registered worktrees (forks plus reviewers plus runs). A program whose
    concurrent width exceeds that needs forks as shared clones instead of worktrees (S1's
    host-independent substrate), or a sandbox profile that does not enumerate worktrees.
  - **The delivery audit charged re-dispatches and new directories three repair agents each.**
    Git collapses the first files of a new directory into one `resp/` status entry, so resp-errors,
    resp-writer, store-core and server-dispatch were audited with every planned file missing; a
    review re-dispatch that changed only `hashes_test.go` was audited missing the `hashes.go` it had
    delivered a wave earlier; and after resp-reader's pushback a repair agent rewrote the file the
    pushback declined to change, which the Lead then discarded. The audit now lists untracked files
    individually, counts a planned path already in HEAD as delivered (`presentPaths`), and a
    `pushback` report runs no repair. Tests in `test_delivery_repair.py`. The retrospective's four
    learnings from this run named the symptoms (never amend a frozen test; Bash is broken on this host)
    rather than the mechanisms and were retracted as operator.
- The sixth proof run (`299b4907`, same input, $43.47, 1 h 51 min) is the first `PROGRAM_DELIVERED`:
  all six subsystems (design, resp, store, server, cmd, integration) built in dependency order, 17 code
  items delivered, `make test` (`go vet` + `go test -race`) green on the program branch, zero task
  failures, zero blocked items. Every fix from the fifth run held live: the new run pruned all seven
  finished runs' worktrees at start (71 registered → 2), every wave removed its decided forks
  (`removedForks` on all 16 integrate steps), zero E2BIG across 92 agent sessions, both approved
  amendments (r-errors, s-core) were delivered at the post-merge head, no repair ran after a pushback,
  and the retrospective's seven learnings named mechanisms. One defect, found by running the
  delivered branch's own frozen acceptance suite by hand — **the harness never ran it at the integrated
  head**:
  - **A frozen test is proven on its fork only.** sv-core's acceptance test asserted that `PING` is an
    unknown command — true when it was written, since no sibling had registered any command — and was
    green on sv-core's fork. sv-connection then registered `PING`; from that wave on the frozen test
    was red on the shared branch (2 of 290 acceptance tests), three implementers and the retrospective
    noticed and said so, and nothing acted: each item's gate runs only its own test on its own fork,
    the team's final `test_cycle` discovers the repository's documented `make test`, which never
    executes the Python acceptance suite, and `team_outcome` only asked whether that cycle passed. The
    program delivered with its oracle red. Three changes: `team_integrate` now re-runs every integrated
    item's frozen tests at the integrated head after each wave's merges (the integration oracle) and
    re-opens a red item with the failing output as its directive — the re-dispatch fixes the code or
    amends the falsified assertion, which the peer reviewer decides as any amendment; the final
    `test_cycle` carries the frozen tests' commands as its `priorVerification` obligation next to
    `make test`; and `team_outcome` reads the commands the final cycle actually ran and refuses a head
    with a frozen test no command names (`acceptanceUnproven`). Acceptance-test authors are told never
    to assert a sibling's behaviour is absent. Tests in `test_team_cycle.py` run a real frozen test at
    a real integrated head.
  - Three of the run's learnings described code, docs and a test that exist only on the unmerged run
    branch (a fresh run starts from `main`) and were retracted; the retrospective prompt now says so.
    c-main's three delivery-repair agents each explained that `main.go` was complete and never created
    the missing `main_test.go` the audit named; the repair brief now leads with the missing paths and
    says the delivery is complete only when they exist.
- The seventh proof run (`a4f1ff1f`, same input, $12.41, 62 min) ended `PROGRAM_INCOMPLETE` at 1/6:
  design delivered (4/4, $4.64) and the breakdown re-sliced, then both rank-1 teams failed
  independently in their first half hour. The integration oracle ran on every wave (`passed` ×18,
  nothing red yet). Three defects, all fixed:
  - **The final test cycle's repair agent ran in the shared worktree.** The store team's loop ended
    (see below) and its final `test_cycle` started with `repoPath` = the shared run worktree while the
    resp team was still integrating into it. The cycle carried every frozen test (including st-core's,
    red by construction since st-core was not done), so it failed and dispatched a repair agent — into
    the shared worktree, with write roots derived from the branch's last commit, which was the resp
    team's. That agent wrote a stub `internal/resp/writer.go`; the agent runner then reset the worktree
    to the head the session had started from, which predated the resp team's r-reader merge, and the
    repair commit was parented there: the program branch lost the r-reader merge, and r-writer's
    approved-amendment fast-forward aborted on the untracked `writer.go`. `team_apply_reviews` raised
    on the git error and the resp team ended as a FAILED task. The final cycle now runs on its own fork
    (`tests_fork`), its obligations are the done items' frozen tests and its write roots the team's own
    span (both read off the board at the end of the loop), `team_outcome` integrates a green repair by
    rebase + fast-forward and discards a red one, and a failed amendment merge rebuilds the item
    instead of failing the team. Tests in `test_team_cycle.py` fast-forward a real repair fork and
    reproduce the merge-failure path.
  - **A scope wait on a dependent was a deadlock.** The breakdown put `type Stats` in st-stats'
    `stats.go` and made st-stats depend on st-core, whose `store.go` needs the type to compile. The
    reviewer asked st-core to declare it in `stats.go`; the Lead held st-core because st-stats owned
    the file, and st-stats could not start until st-core landed. The Lead now widens at once when every
    open owner of the file depends on the waiting item.
  - **The team spun 14 empty waves to `maxWaves` on that hold** (80 seconds, reported as "max waves
    reached"): the liveness check counted a held item as movable. A wave that dispatched nothing
    because every ready item was held, with nothing in flight, now ends the run at once as
    `scope_wait`, each hold posted as an escalation.
  - Two of the run's three learnings described the run branch's own design docs and tests and were
    retracted; the retrospective had been told not to record those the hour before, so the rule is a
    prompt and remains probabilistic.
- The eighth proof run (`90e5e3aa`, same input, $5.90, 31 min) ended `PROGRAM_INCOMPLETE` at 3/6:
  design, resp (3/3) and store (2/2) delivered, every team's final test cycle ran on its own fork and
  removed it, the oracle ran on every wave, zero E2BIG, no scope wait, no stall. The server team
  blocked on an item whose work a sibling had absorbed. Two defects, both fixed:
  - **No team had a frozen acceptance test.** The breakdown agent wrote each item's
    `acceptanceCriteria` as one paragraph; the breakdown schema typed items as bare objects, so the
    payload validated, and the board's fold keeps `[]` for a non-list — every team's acceptance
    author had nothing to author. The schema now spells out each item (criteria as arrays of
    strings, required) so the validator rejects the shape and the agent retries, and criteria given
    as text are split into criteria at the fold and on the board, never dropped.
  - **An item whose work a sibling absorbed had no path to done.** cmd-handlers' write root was its
    single `handlers.go`, so its implementer put the registry and dispatch there; cmd-dispatch's
    implementer found nothing left to write, said so in prose, and changed nothing. Without a frozen
    test the "already satisfied" exit could not fire, the audit ran three repair agents per attempt
    (each re-verified the claim and created nothing), the item blocked as an empty delivery after two
    attempts, and sv-server, cmd and integration starved. The implementer's report now has a
    structured `alreadySatisfied` claim: the audit runs no repair, and the integrator sends the item
    to its peer reviewer with an empty delivery range and the evidence — `lgtm` is done as-is,
    `changes` re-opens it with what is missing or misplaced. Tests in `test_team_cycle.py`,
    `test_delivery_repair.py`, `test_team_board.py`, `test_program_teams.py`. The run's three
    learnings blamed the slicing for the harness defect and were retracted.
- The ninth proof run (`0709d705`, same input, $4.75, 20 min) ended `PROGRAM_INCOMPLETE` at 0/6: the
  docs team delivered three of its four design documents and quarantined the fourth
  (`review_rounds_exhausted`), so no code team started. One defect, fixed:
  - **The Lead learned of a scope gap a wave late, and the wasted round counted.** The store-design
    reviewer's first `changes` named `architecture.md` — outside the item's single file — as needing
    an edit. The Lead widens scope only from an implementer's recorded refusal, which arrived a wave
    later when the re-dispatched implementer tried the edit; the second review then said, in so many
    words, "not a fault of this implementer, the Lead needs to widen", and counted as the item's second
    round. The Lead widened for wave four, the implementer made the edit, and the third review raised
    a new content gap: over the cap, quarantined, program over at rank 0. Reviewers now report changes
    that need files outside the implementer's list under `outOfScope` (file + change); the Lead
    records those files as widening evidence immediately and widens at the next standup; a round that
    asks for nothing else is `scopeOnly` and counts against neither the review cap nor the monitor's
    quality signal; and reviewers are told to raise every blocking problem in one round. Test in
    `test_team_cycle.py` runs the scope-only round, the widening and the cap arithmetic live. Four of
    the run's five learnings blamed the plan or described run-branch docs and were retracted.
- The tenth proof run (`ba2491ff`, same input, $30.10, 1 h 54 min) ended `PROGRAM_INCOMPLETE` at
  3/6: design (6 docs), resp (3/3) and store (8/8) delivered; the server team landed 11 of its 13
  items, including the TCP server, and blocked two one-file command items after 70 minutes. Every
  earlier fix held (13 frozen tests, oracle on every wave, forks for the final cycle, a reviewer's
  `outOfScope` used live, zero E2BIG in 115 sessions). Two defects, both fixed:
  - **An approved item with an out-of-scope change was marked done without the change.** The
    registry item's reviewer returned `lgtm` and, under `outOfScope`, "move the `Handler` type out of
    echo.go" — a sibling's file where the type was already declared, so the package no longer built.
    The verdict closed the item; the widening evidence was recorded but the item was never
    re-dispatched. `lgtm` with `outOfScope` is now a scope-only `changes` round: re-opened, widened at
    the next standup, counted against no cap.
  - **A pushback with a refused edit was treated as a refused constraint.** The integration oracle
    re-opened the first command items whose frozen tests the build break turned red, with the
    directive to make them pass. Each implementer found the cause in the two siblings' files, was
    refused there, and reported pushback naming the files. The Lead treated it as `constraint_pushback`:
    with the oracle's re-open already counted as an attempt, both blocked at the cap on their first
    pushback, and cmd and integration starved. A pushback whose refused edits lie inside the team's span
    is now a scope gap: re-opened at the same attempt with a resolved notice, widened at the next
    standup. Tests in `test_team_cycle.py` for both. The run's three learnings blamed the slicing for
    the harness defects and were retracted.
  - **Bash bypassed the write roots.** The same run showed an implementer removing a sibling's
    declaration through Bash after the Edit tool refused the file: the guard sees Write/Edit, the OS
    sandbox allows any write inside the worktree, and the commit staged every vetted change. That fork
    was discarded for its pushback, but a plain delivery would have integrated an edit nobody owned.
    A fork's commit now stages only changes inside the item's write roots and frozen tests; the rest
    is left unstaged and reported as `outsideScope`, merged into the item's refused writes so the Lead
    widens from it. The test cycle's repair commit is confined the same way. Test in `test_gitops.py`.
- The eleventh proof run (`a57bf979`, same input, $1.97, 15 min) ended `PROGRAM_INCOMPLETE` at 0/6:
  the architecture document, the single rank-0 item, was quarantined after three review rounds. One
  defect, fixed:
  - **The review cap could not tell a rejected item from a review that keeps finding things.** Round
    one raised three findings; the implementer fixed all three. Round two raised one new finding about
    the fix; fixed. Round three raised two new findings about that fix. Nothing was ever left
    unaddressed, but three `changes` verdicts exceeded the cap of two, the item blocked as
    `review_rounds_exhausted`, and nothing else in the program could start. Reviewers now report under
    `unresolved` (required) which of the previous round's findings the revision did not address. Over
    the cap with none unresolved, the Lead accepts the item and carries the new findings to its
    dependents as directives, recording the decision as a resolved `review_cap` notice; with a prior
    finding unaddressed, or no report, it quarantines as before. Test in `test_team_cycle.py` covers
    all three outcomes. Two of the run's four learnings restated the cascade or the run-branch design
    and were retracted; the two about Go export conventions and cross-section consistency stay.
- The twelfth proof run (`b8dc259b`, same input, $1.47, 10 min) reported `PROGRAM_DELIVERED` and built
  nothing: one architecture document, no code. Two defects, both fixed:
  - **The validator accepted a placeholder and the harness believed it.** The planner wrote a
    six-subsystem plan twice as tagged text inside its `summary` string; the output validator rejected
    both for lacking `subtasks`; the planner then sent a one-item plan — description "test", criterion
    "ok", a documents deliverable — which validated. The runner's salvage only ran on a failed session,
    so the placeholder stood. The runner now compares an accepted payload with the rejected attempts:
    one that untangles into a schema-valid payload at least four times the size replaces it.
  - **The gate could not reject a one-item plan.** A plan of one item routed `single` and was never
    sent back to the planner, and nothing checked that a program plan builds any code. Placeholder
    descriptions and criteria, and a program with no `code` deliverable, are now fatal reasons that
    reject a plan of any size and feed the planner its replan prompt. Tests in
    `test_structured_output.py`, `test_coding_agent_policy.py`, `test_decomposition.py`. The run's
    retrospective recorded nothing.
- The thirteenth proof run (`688f426b`, same input, $32.98, 1 h 58 min) is the proven delivery:
  `PROGRAM_DELIVERED` 6/6 — design (4 documents), resp 5/5, store 2/2, server 9/9, cmd 1/1,
  integration 1/1; 22 code items, 31 peer reviews (8 change rounds, no cap reached), one re-dispatch
  (s-errors, below), zero blocked, zero task failures. Checked by hand at the delivered head: all 154
  frozen acceptance tests pass and `make test` is green. Every mechanism added since run 6 ran live:
  the plan and breakdown came through with list criteria and no salvage; every code item had a frozen
  test; the integration oracle ran on every wave and caught one real regression — s-errors' frozen
  test went red when s-core landed, s-errors was re-opened with the failing output, its re-dispatch
  fixed it and the next wave's oracle was green; every team's final test cycle ran on its own fork,
  needed no repair, named every done item's test, and removed the fork; the Lead widened scope six
  times from recorded refusals and refused once (sv-server asking for `cmd/resp-cache/main.go`,
  outside the server team's span); no pushback, no scope wait, no claim, no review-cap decision was
  needed. Registered worktrees ended at 2; zero E2BIG across 89 sessions. Five of the run's six
  learnings described code on the run branch and were retracted; the sixth — a per-file slice must
  own the source file's test file too — is now in the breakdown prompt.
