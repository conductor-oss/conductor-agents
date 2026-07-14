# conductor-code — Detailed Design

> Companion to [`SPEC.md`](SPEC.md) (the *what/why*). This is the *how*.
> **Status: design only — no code written yet.** The build plan is §10.

## Contents

1. [Architecture overview](#1-architecture-overview)
2. [Execution model](#2-execution-model)
3. [Spec adapter & common change model](#3-spec-adapter--common-change-model)
4. [Conductor workflow definitions](#4-conductor-workflow-definitions)
5. [Task definitions to register](#5-task-definitions-to-register)
6. [Skill structure & command flows](#6-skill-structure--command-flows)
7. [Bootstrapping & prerequisites (`adopt`)](#7-bootstrapping--prerequisites-adopt)
8. [Observability, scheduling, resume](#8-observability-scheduling-resume)
9. [Risks & open decisions](#9-risks--open-decisions)
10. [Phased build plan](#10-phased-build-plan)
11. [End-to-end verification](#11-end-to-end-verification)

---

## 1. Architecture overview

The harness orchestrates a **fleet of CLI coding agents** (Claude Code, Codex) as
durable Conductor workers. Three roles:

```
┌──────────────────────────────────────────────────────────────────────┐
│  DEVELOPER MACHINE (where the git repo lives)                          │
│                                                                        │
│  conductor-code skill          workers (poll Conductor; FS/git access) │
│  (Claude Code, interactive)     • agent worker → wraps CLI coding agents│
│   • detect spec framework          claude (Claude Code) · codex (Codex) │
│   • start execution                each runs its OWN internal loop      │
│   • surface + release gates      • run_checks / security_scan / git_ops │
│   • stream status + budget       • write_artifact / detect_and_load / ci│
│        │   ▲                      • enforce guardrails + budgets         │
└────────┼───┼──────────────────────────────┼───┼─────────────────────────┘
         │   │ start / signal / poll         │   │ poll / complete
         ▼   │                               ▼   │
┌──────────────────────────────────────────────────────────────────────┐
│  CONDUCTOR SERVER (durable orchestrator — local or Orkes)              │
│   • lifecycle + per-type sub-workflows; profile-gated optional phases  │
│   • routes each phase to the right AGENT (claude/codex) by role        │
│   • WAIT/HUMAN gates: 2 fixed + dynamic escalation (resolve in UI)      │
│   • FORK_JOIN_DYNAMIC fan-out across agents (scans, parallel tasks)     │
│   • NO LLM provider keys needed — agents authenticate locally          │
│   • audit (which agent+model did what) + scheduler (nightly find-bug)  │
└──────────────────────────────────────────────────────────────────────┘
```

**Division of labor**

| Concern | Owner |
|---|---|
| Developer UX, gate presentation, releasing gates | `conductor-code` skill (local, interactive) |
| Durable lifecycle state, retries, gates, fan-out, audit, scheduling, **routing to agents** | Conductor server |
| **All model-powered work** — spec/plan/root-cause/scan/edit/review | **`agent` worker** wrapping a CLI coding agent (Claude Code / Codex) |
| Deterministic side effects — checks, security scan, git, artifact writes | Utility workers (`run_checks`, `security_scan`, `git_ops`, `write_artifact`, …) |
| Pure plumbing — routing, counters, profile rules, JSON merge | `INLINE` / `JSON_JQ_TRANSFORM` / `SWITCH` (no agent, no LLM) |
| Translating the change model ↔ Spec-Kit / OpenSpec files | Spec adapter (a library used by the workers) |
| Safety (guardrails, budgets); profile + (agent,model) selection | Workers (pre-flight) + `select_profile`/`resolve_models` steps |

### Why Conductor (the value proposition)

Conductor is the durable orchestrator for a **heterogeneous fleet of CLI agents**:

- **Routes each phase to the right agent** by role (architect/builder/reviewer/…),
  and runs them **in parallel** — including a **planner-decomposed DAG of sub-agents
  executed in dependency waves** (`FORK_JOIN_DYNAMIC` of sub-workflows) for large
  changes, and parallel bug scans.
- **Durable & resumable** — a multi-hour run across many agent invocations survives
  crashes/restarts; resume from the last completed task.
- **Human-in-the-loop (`HUMAN` tasks)** — fixed gates *and* dynamic escalations are
  Conductor `HUMAN` tasks that **notify/assign a person and collect structured input**
  (approve / edit / feedback / answer), resolvable from the terminal or the Orkes UI.
- **Self-improving review + cross-agent diversity** — every coding sub-agent runs a
  `build↔review` loop (reviewer = a *different* agent); non-convergence climbs an
  agent/model escalation ladder before a human, and a final review validates the whole
  change vs spec/design.
- **Observability, audit & reproducibility** — every phase records which
  **agent + model + version** ran, its prompt, gate decisions, budget, and results.
- **Scheduling** — autonomous nightly `find-bug`.

---

## 2. Execution model

### 2.1 Agent workers — CLI coding agents wrapped as workers

The harness makes **no raw LLM calls**. Every model-powered step — drafting a spec,
planning, root-cause, scanning, editing, reviewing — is performed by a **CLI coding
agent wrapped as a Conductor SIMPLE-task worker**. A single `agent` worker dispatches
to the agent named in its input — **Claude Code (`claude -p`)** or **Codex
(`codex exec`)** — through an **agent adapter** that normalizes invocation and
output. v1 supports `claude` and `codex`; the adapter lets us add others
(`gemini`, `aider`, …) without touching workflows.

```
in:  { agent, model, effort, mode, prompt, repoPath, schema? }
out: { result | diff, filesTouched, summary, findings?,
       metadata: { agent, model, version, tokens, costUsd } }
```

- The agent runs its **own internal ReAct loop** (gather context → plan → edit → run
  tests → iterate) within a single invocation. Conductor orchestrates *across*
  invocations — phases, gates, fan-out, budgets, escalation — and does **not**
  micro-manage individual edits (see §4.6).
- **Modes** are read-only or writing. Read-only "reason" modes (`clarify`, `spec`,
  `design`, `plan`, `root_cause`, `scope`, `analyze`, `review`, `acceptance`,
  `dedupe_rank`, `scan`) produce structured artifacts/answers and are **denied write
  access by the guardrails**. Writing modes (`implement`, `reproduce`,
  `regression_test`, `document`, `examples`) edit the repo on a branch.
- **Structured output** (replacing `LLM_CHAT_COMPLETE`'s `jsonOutput`/`outputSchema`):
  the adapter embeds the JSON schema in the prompt, parses the agent's final message
  (`claude -p --output-format json` / `codex exec` JSON), validates, and retries
  (bounded) on mismatch.

Lighter **utility workers** (`detect_and_load`, `write_artifact`, `run_checks`,
`security_scan`, `git_ops`, `wait_ci`) do deterministic side effects without an
agent.

> **No LLM provider keys on the Conductor server** — agents authenticate **locally
> on the worker host** (`claude` logged in; `codex` logged in / `OPENAI_API_KEY`).

### 2.2 Why local workers

Workers are just processes that poll the Conductor server, so they run on the machine
that holds the repo and the authenticated agent CLIs — freely reading/editing files,
running checks, and using git, with no need to ship the repo to the server. v1 ships
**TypeScript** workers (`@io-orkes/conductor-javascript`, `TaskManager`); the `agent`
worker shells out to `claude` / `codex`.

Workers are **stateless & idempotent** (Conductor may redeliver on timeout): writes
are branch-scoped and safe to re-run.

### 2.3 State lives in three layers

| Layer | Holds | Source of truth for |
|---|---|---|
| **Conductor execution** | live phase, variables (`profile`, agent/model per role, budget counters), gate status, prompts, agent+model ids, check results, audit | *process* state (what phase, what's pending, what was spent, which agent ran) |
| **Spec files in the repo** | spec/proposal, plan, tasks, deltas, docs, examples, ADRs | *human-readable artifacts* (written by the adapter / `agent`) |
| **`.conductor-code/`** | `config.json` (specFormat, approver, gate prefs), `policy.json` (guardrails + budget caps), `models.json` (role → agent/model/effort), `changes.json` (changeId → workflowId) | local config + the change↔execution mapping used by `status` and gate-release |

### 2.4 Safety, budgets & reproducibility (cross-cutting)

These wrap *every* phase; mechanics in §4.9.

- **Guardrails** — side-effecting workers (`agent` write modes, `write_artifact`,
  `git_ops`) load `.conductor-code/policy.json` and run **pre-flight checks** before
  any write/command: protected-path globs, secret patterns, a destructive-command
  denylist, blast-radius cap, and **read-only enforcement** for reason/scan modes. A
  violation routes the workflow to an escalation gate.
- **Budgets** — per-run caps (iterations, tokens, files, $cost) in
  `workflow.variables`, bumped after each step; loop conditions + post-phase
  `SWITCH`es check them and escalate on breach. Runtime deadlines live only on
  Conductor task definitions.
- **Reproducibility** — each `agent` invocation returns `{agent, model, version,
  tokens}`; `git_ops` records the commit sha — all in the Conductor audit. Re-run via
  `conductor workflow rerun`.

### 2.5 Agent & model policy (multi-agent, multi-model)

Work is assigned by **role**, each mapping to an **(agent, model, effort)** —
front-load expensive reasoning on a large model, run execution on cheaper/faster
agents, and review with a *different* agent for diversity.

| Role | Default agent · model | Effort | Phases / tasks |
|---|---|---|---|
| **architect** | Claude Code · `claude-opus-4-8` | max thinking | clarify · draft_spec · design · draft_plan · root_cause · analyze · acceptance |
| **builder** | Claude Code · `claude-sonnet-4-6` *(or Codex)* | medium | implement · reproduce · write_regression · scan_area |
| **reviewer** | **the other agent** (e.g. Codex when built with Claude) | high | code_review · adversarial verify |
| **scribe** | Claude Code · `claude-haiku-4-5` | low | document · examples · changelog · commit messages |

- **Resolution** — `resolve_models` (INLINE, after `select_profile`) writes
  `workflow.variables.{agent,model,effort}_<role>` with precedence **run-input →
  profile → `models.json` → shipped defaults** (e.g. `trivial` downgrades
  `architect`→Sonnet; `full` keeps Opus·max).
- **Single injection point** — every agent/model decision flows into the `agent`
  worker's `inputParameters`; there are no server-side LLM tasks to template.
- **Cross-agent diversity** — `reviewer` defaults to a *different* agent than the
  `builder` that wrote the code; same idea in `find-bug` (scan with one, verify with
  another).
- **Escalation ladder** — the `builder` starts cheap (OSS/Sonnet) and is promoted —
  Opus·high → Opus·max + raised budget, optionally swapping agent — only when it isn't
  converging (tests red *or* the group **reviewer** keeps finding issues, §4.6). Last
  rung is a HUMAN gate. Configurable in `models.json`; the `reviewer` is always a
  *different* agent than the `builder`.
- **All in one file** — agents · tiers · role→tier + ladders · per-profile overrides ·
  the `reviewLoop` hill-climb (score weights, `target`, `plateau`) · budgets · prices
  live in `.conductor-code/models.json`. Schema + defaults:
  [`docs/config/models.example.json`](config/models.example.json).

### 2.6 Verification depth & test environments

A green unit build is necessary but not sufficient for real runtime behavior
(concurrency, distributed state), so the verification suite is **pluggable, tiered,
and feeds the hill-climb score directly** (`checks.json`,
[example](config/checks.example.json)):

- **Checks registry** — named checks, each with a `kind` (unit · typecheck · lint ·
  build · race · integration · property · consistency/linearizability ·
  fault-injection/chaos · load), a `cmd`, a `weight`, `blocking`, `needsEnv`, and
  non-determinism handling (`nonDeterministic` + `reruns`).
- **Altitudes** (`when`) — `per-round` (cheap/deterministic: unit, typecheck, race —
  inside the group hill-climb so the score moves every iteration), `per-wave`
  (integration/property after each `integrate`), `final` (expensive distributed checks:
  linearizability, chaos, load — in `verify_workflow`). Keeps Jepsen out of the inner
  loop.
- **Test environments** — `needsEnv` checks run against a **real running system**: the
  **`provision_env`** worker stands up an N-node cluster/containers (`environment.up`)
  before and tears it down after. "Verify" means *run the distributed system under
  fault/load*, not compile it.
- **System invariants** — the `design` declares explicit invariants (linearizable
  writes, no-loss-on-single-failure, idempotent retries…), each mapped to the check(s)
  that prove it; verified at the **walking skeleton** (§4.2), per-wave, and the final
  review. A failing invariant bounces back to **design**, not just `implement`.
- **Non-determinism handling** — `nonDeterministic` checks are re-run `reruns` times
  and scored by pass-rate; deltas within `flake.noiseBand` don't count as improvement,
  so flaky results can't fool **keep-best** or **plateau** detection
  (`reviewLoop.plateau.minImprovement ≥ noiseBand`).

---

## 3. Spec adapter & common change model

A library (`scripts/adapter/`) maps the internal **change model** (see SPEC §
"Common change model") to/from each framework's files. All workflow logic is written
against the change model; only the adapter knows the frameworks.

**Detect** (run by `detect_and_load`):

| Found in repo | Mode |
|---|---|
| `openspec/` | OpenSpec |
| `.specify/` and/or `specs/<branch>/` | Spec-Kit |
| neither | prompt once → default **OpenSpec**, persist to `.conductor-code/config.json` |

**Adopt existing** — `detect_and_load` reads an in-progress OpenSpec change
(`openspec/changes/<id>/`) or Spec-Kit feature (`specs/<branch>/{spec,plan,tasks}.md`)
into the change model so a run can resume mid-stream instead of starting over.

**Artifact mapping**

| Change-model field | Spec-Kit path | OpenSpec path |
|---|---|---|
| intent | top of `specs/<branch>/spec.md` | `changes/<id>/proposal.md` |
| requirements (+ acceptance) | `specs/<branch>/spec.md` | `changes/<id>/specs/**` (delta) → merged to `openspec/specs/<cap>/spec.md` on archive |
| design (contracts/data-model) | `contracts/`, `data-model.md`, `research.md` | `changes/<id>/design.md` |
| invariants (system properties) | `spec.md` / `plan.md` (Invariants) | `changes/<id>/design.md` (Invariants → checks) |
| plan (+ test strategy, risk) | `plan.md` | `changes/<id>/design.md` (plan section) |
| tasks | `tasks.md` | `changes/<id>/tasks.md` |
| deltas | (implicit per branch) | `changes/<id>/specs/**` ADDED/MODIFIED/REMOVED |
| artifacts (docs/examples) | written in place (`README`, `docs/`, `examples/`, `CHANGELOG`) | same (written in place) |
| decisions (ADRs/learnings) | `docs/adr/`, `memory/constitution.md` | `openspec/project.md`, `changes/<id>/design.md` |
| principles | `memory/constitution.md` | `openspec/project.md` |

---

## 4. Conductor workflow definitions

One **parent lifecycle** workflow selects a profile + agent/model map and dispatches
to three **sub-workflows**. All share `detect_and_load`, `select_profile`,
`resolve_models`, the two fixed gates, profile-gated optional phases, `verify`, and
`archive`. Every model-powered step below is an **`agent` worker** invocation (role
in parentheses); there are **no `LLM_CHAT_COMPLETE` tasks**.

### 4.1 Parent: `conductor_code_lifecycle`

```
input: { kind, request, repoPath, specFormat, approver, branch, profile?, agents? }
  detect_and_load   (SIMPLE)     → context, specFormat, existing change, policy
  select_profile    (INLINE)     rules on kind+size → trivial|standard|full (input overrides)
  resolve_models    (INLINE)     defaults ← models.json ← profile ← input
                                 → variables.{agent,model,effort}_<role>
  route             (SWITCH on kind)
    ├ "feature"  → SUB_WORKFLOW feature_workflow
    ├ "fix"      → SUB_WORKFLOW fix_workflow
    └ "find_bug" → SUB_WORKFLOW find_bug_workflow
output: { changeId, branch, artifacts, verification, integration, budgetUsed }
```

### 4.2 `feature_workflow`

Phases in `[brackets]` are wrapped in a profile `SWITCH` (§4.8) and skipped when the
profile disables them.

```
[clarify]          (agent · architect, read-only)  full: resolve open questions (may escalate)
draft_spec         (agent · architect, read-only)  spec + acceptance criteria (structured out)
write_spec         (SIMPLE write_artifact)         adapter writes spec to framework path
─ GATE 1: revise-loop (DO_WHILE) ───────────────────────────────────────────
    approve_spec   (HUMAN)                         notify + collect: decision/feedback/edits/profile
    revise         (SWITCH on decision)            decision:"changes" → architect re-drafts w/ feedback
─────────────────────────────────────────────────────────────────────────────
draft_plan         (agent · architect, read-only)  approach + test strategy + risk & rollback
[design]           (agent · architect, read-only)  full: contracts / data-model / interfaces
draft_tasks        (agent · architect, read-only)  tasks as a DEPENDENCY DAG: dependsOn + write-scope + contracts
write_plan_tasks   (SIMPLE write_artifact)
[analyze]          (agent · architect, read-only)  consistency: spec↔plan↔tasks; DAG acyclic + write-scopes disjoint
─ GATE 2: approve plan + design + invariants (HUMAN) ── before ANY edits ────
[skeleton]         (agent · builder + provision_env) arch-bearing/full: build a thin END-TO-END
                                                     vertical slice; validate vs invariants on a real
                                                     env; FAIL → bounce to design (re-GATE 2) before fan-out
implement          (sequential OR parallel waves)  §4.6 — profile-gated; budget-capped
verify             (SUB_WORKFLOW verify)           final review on a provisioned env (§4.7)
verify             (SUB_WORKFLOW verify)           broadened matrix; cross-agent review (§4.7)
[document]         (agent · scribe)                docs / README / API docs / CHANGELOG
[examples]         (agent · scribe)                usage examples / quickstart / demos
[integrate]        (git_ops → wait_ci)             branch · commits · PR + description · CI
archive            (SIMPLE)                        merge deltas → truth specs; write ADR; summary
```

### 4.3 `fix_workflow`

```
reproduce          (agent · builder, write)        reproduce the bug; capture failing behavior
root_cause         (agent · architect, read-only)  analyze repro + context
draft_fix          (agent · architect, read-only)  minimal-change proposal + severity → may bump profile
─ GATE 1: approve_fix (HUMAN) ──────────────────────────────────────────────
write_regression   (agent · builder, write)        write a FAILING test capturing the bug
run_checks         (SIMPLE)                         confirm RED
implement_fix      (DO_WHILE; agent · builder)      edit until regression test + suite are GREEN
verify             (SUB_WORKFLOW verify)
[document]         (agent · scribe)                 if behavior/docs are affected
[integrate]        (git_ops → wait_ci)
archive            (SIMPLE)                          document fix + ADR; merge to specs
```

### 4.4 `find_bug_workflow` (read-only by default — fan-out + cross-agent showcase)

```
scope              (agent · architect, read-only)  partition codebase into N areas × lenses
                                                    → emits dynamicTasks[] + dynamicTasksInput{}
scan_fork          (FORK_JOIN_DYNAMIC)             N parallel scan (agent · builder, scan mode)
  └ join
dedupe_rank        (agent · architect, read-only)  merge + dedupe + rank by severity/confidence
verify_fork        (FORK_JOIN_DYNAMIC)             per finding: adversarial verify (agent · reviewer
  └ join                                            = a DIFFERENT agent than scanned)
report             (SIMPLE write_artifact)         ranked findings report (optional GENERATE_PDF)
─ GATE: approve_proposals (HUMAN) ─ human picks which to convert ────────────
spawn_fixes        (per approved finding)          START_WORKFLOW fix_workflow (fire-and-forget)
```

This is the **read-only instance of the §4.6 `decompose → fan-out → integrate`
primitive**: `scope` = decompose, the parallel scans = fan-out, `dedupe_rank` =
integrate, `verify_fork` = verify. Read-only is **guardrail-enforced** (`scan` is a
read-only agent mode). Cross-agent verification (scan with `builder`, refute with
`reviewer` = the other agent) cuts single-agent false positives. `scope` output feeds
the dynamic fork:

```json
// dynamicTasks
[{ "name": "agent", "taskReferenceName": "scan_users", "type": "SIMPLE",
   "inputParameters": { "mode": "scan", "area": "users", "lens": "security" } }]
// dynamicTasksInput
{ "scan_users": { "mode": "scan", "agent": "${...agent_builder}", "area": "users",
                  "lens": "security", "repoPath": "..." } }
```

### 4.5 Gates & human-in-the-loop (`HUMAN` task)

All human checkpoints — the two fixed gates, find-bug proposal selection, the optional
pre-archive sign-off, and **dynamic escalations** — are Conductor **`HUMAN` tasks**.
Unlike a bare `WAIT`, a `HUMAN` task is built to **notify/assign a person and collect
structured input**:

- **Notify** — assigned to the approver (and can fire a notification); the skill also
  surfaces it in the terminal, and it appears as a form in the Orkes UI.
- **Collect input** — the human returns a structured payload, not just yes/no:
  `{ decision: approve|changes|reject, feedback, edits?, answers?, profile? }`.

```json
{ "name": "human_gate", "taskReferenceName": "approve_spec", "type": "HUMAN",
  "inputParameters": { "assignee": "${workflow.input.approver}",
                       "title": "Approve spec for <change>", "artifactRef": "spec" } }
```

Released by completing the task with the input (terminal `signal-sync` or the UI form):

```bash
conductor task update-execution --workflow-id {wfId} --task-ref-name approve_spec \
  --status COMPLETED --output '{"decision":"changes","feedback":"...","profile":"full"}'
```

Downstream reads `${approve_spec.output.decision}` / `.feedback`. The revise-loop
(§4.2) wraps `[approve_spec → revise]` in a `DO_WHILE` so `decision:"changes"` cycles
back to an architect re-draft. Set generous `responseTimeoutSeconds`. The same `HUMAN`
task also powers **escalations** (§4.9) — used to *ask the human a question* (low
confidence, guardrail trip, budget breach, non-converging review) and get a typed
answer, not just an approval.

### 4.6 `implement` engine — sequential or planner-driven parallel

Coding agents run their **own** plan→edit→test loop, so Conductor orchestrates at
**task granularity**, not per-edit. A `SWITCH` on `profile` selects the engine:

- **trivial / standard → sequential** — a single `builder` agent.
- **full / large → parallel** — walk the approved task **DAG** in dependency-ordered
  **waves** of parallel sub-agents.

**Sequential** (small changes — the agent's own loop does the heavy lifting):

```
DO_WHILE (graaljs; stop when tasks done OR a budget cap hit)
  pick_task  (INLINE)        next ready task from the DAG + state
  do_task    (SIMPLE agent)  builder implements it end-to-end   · model_implement (mutable)
  run_checks (SIMPLE)        scoped tests/typecheck
  record     (SET_VARIABLE)  bump counters; mark done/failed
```

**Parallel** (full/large) — the DAG was produced by `draft_tasks` and approved at
GATE 2; here we just schedule and spawn it:

```
DO_WHILE over waves (graaljs; stop when all groups done OR budget hit OR abort):
  next_wave   (INLINE)            ready = deps-done & not-done & write-disjoint within this wave
  fan_out     (FORK_JOIN_DYNAMIC) one implement_group SUB_WORKFLOW per ready group
    └ JOIN
  integrate   (SIMPLE git_ops + agent · merge)  merge group branches → change branch;
                                                 agent resolves residual conflicts
  verify_wave (SIMPLE run_checks)               build/test the integrated state
  record      (INLINE/JQ)         aggregate per-group {cost,status} FROM THE JOIN OUTPUT
                                  (never via concurrent SET_VARIABLE); mark groups done/failed
  [re_plan]   (agent · architect) on failures: re-decompose remaining/failed work given new state
loopCondition: undoneGroups > 0 && budgetOk && !abort
```

**`implement_group_workflow`** — the dynamically-spawned "sub-agent" runs a
**hill-climbing `build ↔ review` loop** (generator + critic). It climbs a measurable
**score** (from `reviewLoop.score` in `models.json`, §2.5), **keeps only improving
moves**, and escalates the builder up its tier `ladder` when it **plateaus** on a local
optimum:

```
setup_worktree (SIMPLE git_ops)         fresh worktree/branch; best = ∅, bestScore = 0
DO_WHILE (hill climb; graaljs):
  do_task    (SIMPLE agent · builder)   revise toward the LARGEST deficit (failing tests / top
                                        blocking findings); WRITE-SCOPED; handed spec+design+contracts
  self_check (SIMPLE run_checks)        per-round checks (unit · typecheck · race) from checks.json
  review     (SIMPLE agent · reviewer)  DIFFERENT agent vs spec/design/contracts + acceptance
                                        → findings[] with severity
  score      (INLINE)                   weighted scalar ∈ [0,1] from reviewLoop.score.signals
  keep_best  (SWITCH + git_ops)         score > bestScore → commit as new best (bestScore = score);
                                        else REVERT worktree to best  (reject the regression)
  plateau?   (SWITCH)                   no gain ≥ minImprovement for plateau.rounds →
                                        promote builder one rung up `ladder` (↑ model + budget),
                                        optionally swap agent; reset the stall counter
loopCondition: bestScore < target && rounds < maxRounds && budget ok
exit: bestScore ≥ target → success ;  ladder & budget exhausted → status = blocked
return { branch, diff, filesTouched, bestScore, status, reviewSummary, costUsd }  ← soft-fail; never aborts the JOIN
```

**Why hill climbing (not "edit until green"):** the **score** + **keep-best /
reject-regressions** stop a revision that breaks a passing test from being kept, and
**plateau detection** turns "stuck" into a signal. The **escalation `ladder` is the
local-optimum escape** — when a cheap tier plateaus *below* `target`, a stronger model
(bigger search radius) takes over; the last rung is a HUMAN gate. Every knob (score
weights, `target`, `plateau`, `ladder`, caps) lives in `models.json` (§2.5); the
checks behind the score — and which run per-round vs per-wave/final — live in
`checks.json` (§2.6).

**Why this shape (the rigor):**
- **DAG + waves**, not a flat list — respects edit dependencies; only intra-wave work is parallel.
- **Contracts are the integration seam** — the `design` phase's contracts are attached
  to each group so independently-built pieces compose (this is why `design` is on by
  default for `full`).
- **Write-ownership + worktrees** — the planner partitions files so groups are
  write-disjoint where possible (disjoint → clean merge); worktree isolation + the
  agent-resolved `integrate` is the safety net for residual overlap.
- **Budget at the JOIN** — parallel branches must not race on `workflow.variables`;
  per-group cost/usage is summed from the join output.
- **Soft-fail + re-plan + escalation** — a failing group returns a status (doesn't
  abort the wave); the coordinator re-plans remaining work or raises a human escalation
  gate; agent/model escalation applies inside each group.

**Agent/model escalation** (both engines): driven by the `builder.ladder` in
`models.json` — climbed on a score **plateau** (tests stay red *or* the reviewer keeps
finding issues) — bumping model/thinking/budget and optionally swapping agent before a
HUMAN gate.

**v1 scope:** flat **one level** of decomposition (groups → waves); no recursive
re-decomposition (deferred — §9 O6).

### 4.7 `verify_workflow` — final review (shared, broadened, cross-agent)

This is the **final, global review** of the *integrated* change against the **whole
spec + design** — the group-level reviews (§4.6) were local to each slice, and
`verify_wave` only confirmed the build stayed green. The check matrix runs in parallel
where independent (`FORK_JOIN` → `JOIN`); profile-gated:

```
provision_env     (SIMPLE)            stand up the N-node system under test (for needsEnv checks)
FORK_JOIN
  ├ run_checks    (SIMPLE)            FINAL altitude (checks.json): unit · race · integration · property ·
  │                                   linearizability · chaos · load (flake-reruns as configured)
  ├ security_scan (SIMPLE)            dependency/vuln scan · secret scan
  ├ analyze       (agent · architect) consistency / drift: code ↔ plan ↔ spec
  ├ invariants    (agent · architect) each declared invariant holds, backed by its mapped checks
  └ code_review   (agent · reviewer)  reviewer = a DIFFERENT agent than the builder (+ optional dg lens)
JOIN
acceptance        (agent · architect) diff vs change-model acceptance criteria
teardown_env      (SIMPLE provision_env · down)
[gate]            (HUMAN, full)       optional pre-archive human sign-off
output: { passed, checks{…}, invariants{…}, drift, reviewFindings }
```

**Review at three levels:** group (§4.6 — local, vs the group's slice), wave (build
stays green after each merge), and final (here — holistic, vs the full spec/design +
**system invariants**, on a real provisioned env). Bounce-back is **graded**: a failing
**invariant** is an architecture problem → bounce all the way to **design** (re-GATE 2);
a lesser code finding → re-enter `implement` for the affected groups; then a HUMAN gate
if still unresolved. Only a green final review unblocks `archive`.

### 4.8 Change profiles & optional-phase gating

`select_profile` sets `workflow.variables.profile ∈ {trivial, standard, full}` from
`kind` + size signals (estimated files/lines, change-type), overridable by
`input.profile` or by the human at GATE 1. Each optional phase is wrapped in a
`SWITCH` on the profile; the matching case runs the phase, `defaultCase: []` skips it:

```json
{ "name": "gate_document", "taskReferenceName": "gate_document",
  "type": "SWITCH", "evaluatorType": "value-param",
  "expression": "profile",
  "inputParameters": { "profile": "${workflow.variables.profile}" },
  "decisionCases": {
    "standard": [ { "type": "SIMPLE", "name": "agent", "taskReferenceName": "document", "inputParameters": { "mode": "document", ... } } ],
    "full":     [ { "type": "SIMPLE", "name": "agent", "taskReferenceName": "document", "inputParameters": { "mode": "document", ... } } ]
  },
  "defaultCase": [] }
```

Profile → optional phases (matches SPEC §"Change profiles"):

| Profile | clarify | design | analyze | document | examples | integrate | gates |
|---|:--:|:--:|:--:|:--:|:--:|:--:|---|
| trivial | – | – | – | – | – | ✓ | auto / 1 light |
| standard | – | – | ✓ | ✓ | – | ✓ | GATE 1+2 |
| full | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | GATE 1+2 (+pre-archive) |

### 4.9 Cross-cutting enforcement → Conductor mechanics

| Policy | Mechanism |
|---|---|
| **Guardrails** | Workers load `.conductor-code/policy.json` and pre-flight every write/command (protected globs, secret patterns, destructive-command denylist, blast-radius cap, read-only enforcement for reason/scan modes). Violation → worker returns `{action:"escalate", reason}`; a `SWITCH` routes to an escalation gate. |
| **Budgets** | Counters in `workflow.variables` (`iterations`, `tokens`, `filesTouched`, `costUsd`) bumped via `SET_VARIABLE`/`JSON_JQ_TRANSFORM`. `DO_WHILE` loop conditions + post-phase `SWITCH`es check caps; breach → escalation gate. Token/cost come from each `agent` invocation's metadata; **`costUsd` is priced per agent/model** (rates in `models.json`). In parallel waves, per-group usage is **aggregated at the JOIN** (concurrent branches never write shared variables). |
| **Escalation** | A reusable conditional `WAIT`/`HUMAN` gate (same release path as §4.5) raised when a worker requests it, confidence is low, a budget trips, or the agent/model escalation ladder is exhausted — distinct from the two fixed gates. |
| **Agent/model escalation** | Before escalating to a *person*, bump the model and/or **swap agent** (Claude↔Codex) for retries (§4.6) — escalate compute/diversity first. |
| **Reproducibility** | Per-task `{agent, model, version, tokens}` recorded in the Conductor audit (returned by the `agent` worker); commit sha by `git_ops`. `conductor workflow rerun` replays. |

---

## 5. Task definitions to register

> **Conductor Rule 1:** every `SIMPLE` task must have a registered task definition
> (exact-name match) or the workflow hangs. `SWITCH`, `WAIT`, `HUMAN`, `DO_WHILE`,
> `FORK_JOIN[_DYNAMIC]`, `JOIN`, `SUB_WORKFLOW`, `START_WORKFLOW`, `SET_VARIABLE`,
> `JSON_JQ_TRANSFORM`, `INLINE` are system tasks and need none. `select_profile`,
> `resolve_models`, `pick_task`, and routing are `INLINE`/`SWITCH`. **The design uses
> no `LLM_CHAT_COMPLETE`** — all reasoning goes through the `agent` worker.

SIMPLE tasks to register (via `conductor task create`):

| Task | Worker | Side effects |
|---|---|---|
| `detect_and_load` | local | read repo + specs + `config.json`/`policy.json`/`models.json` |
| `agent` | local | **wraps a CLI coding agent** (`claude -p` / `codex exec`) chosen by `inputParameters.agent`; read-only modes (clarify/spec/design/plan/root_cause/scope/analyze/review/acceptance/dedupe_rank/scan) or write modes (implement/reproduce/regression_test/merge/document/examples); **enforces guardrails** |
| `write_artifact` | local | adapter writes spec/plan/tasks/report/docs; guardrail-checked |
| `run_checks` | local | runs the `checks.json` suite filtered by altitude (per-round / per-wave / final); kinds: unit · typecheck · lint · build · race · integration · property · consistency · chaos · load; re-runs non-deterministic checks; → structured result + score signal |
| `provision_env` | local | stand up / tear down the N-node system under test (`environment.up`/`down`, readyCheck) for `needsEnv` checks |
| `security_scan` | local | dependency/vuln + secret scan |
| `git_ops` | local | branch · **worktree setup** · commit · diff · **merge group branches** · open PR + description · link issue; guardrail-checked |
| `wait_ci` | local | poll the PR's CI until conclusive (GitHub Actions first) |

`explore`, `clarify`, `design`, `do_task`, `reproduce`, `scan_area`, `analyze`,
`code_review`, `acceptance`, `document`, `examples` are all the **`agent`** worker
with a different `inputParameters.mode` (and per-role `agent`/`model`), so the worker
surface stays tiny. `perf_check` is a `run_checks` mode. `merge`/conflict-resolution
and `re_plan` are `agent` modes (architect for `re_plan`); `review` runs as the
`reviewer` role — always a *different* agent than the `builder`. **`implement_group_workflow`**
is a SUB_WORKFLOW spawned via `FORK_JOIN_DYNAMIC` (no taskDef); `next_wave`/`pick_task`
scheduling is `INLINE`. *(Fallback if dynamic-fork-of-SUB_WORKFLOW is unsupported on
the target version: a SIMPLE `implement_group` task wrapping the same lifecycle.)*
`adopt` registers all task defs + workflow defs and runs `conductor task list` to
verify (Rule 1).

> **Validated in Phase 0 (build spike):** the real CLI verbs are `conductor task
> create|get|list` (not `taskDef`) and `conductor workflow get-execution <id> -c` for
> task detail (`workflow status` 404s on OSS); `codex exec` **must be run with stdin
> closed** or it blocks; both agents support schema-enforced output (claude
> `--json-schema '<inline>'`, codex `--output-schema <file> -o <file>`); the worker
> attributes changes via a **baseline git-diff** (before/after), not absolute status;
> and `conductor workflow create` **won't overwrite** an existing name+version — bump
> the version or delete-then-create when a def changes. Release `HUMAN`/`WAIT` gates
> with `conductor task update-execution --task-ref-name <ref> --status COMPLETED
> --output '<json>'` (`task signal-sync` returns 500 on this OSS build).

---

## 6. Skill structure & command flows

```
conductor-code/                      # the skill (published to conductor-skills marketplace)
├── SKILL.md                         # description + when-to-use + command index
├── commands/                        # one prompt-spec per slash command
│   ├── adopt.md   feature.md   fix.md   find-bug.md
│   └── explore.md verify.md    status.md
├── workflows/                       # Conductor JSON (registered by adopt)
│   ├── lifecycle.json
│   ├── feature.json  fix.json  find-bug.json  verify.json
│   └── taskdefs/*.json
├── scripts/
│   ├── bootstrap.ts                 # register defs + start local worker poller
│   ├── workers/                     # TaskManager workers (agent, run_checks, git_ops, …)
│   ├── agents/                      # agent adapter: claude.ts, codex.ts (uniform invoke + parse + schema-validate)
│   ├── adapter/                     # spec-kit ⇄ change-model ⇄ openspec
│   ├── policy.ts                    # guardrail + budget checks (shared by workers)
│   └── gate.ts                      # poll execution, present artifact, signal release
├── policy.defaults.json             # seed guardrails/budgets copied into .conductor-code/
├── models.defaults.json             # seed tiers + roles/ladders + reviewLoop + prices (schema: docs/config/models.example.json)
└── references/                      # design notes, change-model schema, prompts
```

**Per-command flow** (all of `feature`/`fix`/`find-bug` share this shape):

1. Ensure prerequisites (server reachable, defs registered, worker running, agent
   CLIs authenticated) — else guide the user to run `adopt`.
2. Gather the request; `conductor workflow start -w conductor_code_lifecycle -i
   '{"kind": "...", "request": "...", "repoPath": "...", "approver": "...", "profile": "auto"}'`.
3. Record `changeId → workflowId` in `.conductor-code/changes.json`.
4. Poll `conductor workflow get-execution {id}`; stream phase progress + which agent +
   budget to the user.
5. On a `WAIT`/`HUMAN` gate (fixed or escalation): fetch the pending artifact/question,
   present it, collect approve / edit / answer / reject, and `conductor task signal-sync …`.
6. On completion: summarize artifacts, diff, check results, PR link, and spec path.

- **`explore`** / **`verify`** start the run "up to" a phase
  (`workflow start … --sync -u <taskRef>`) or run the shared `verify_workflow`
  standalone against an existing change.
- **`status`** lists tracked changes and queries each execution's current phase/gate,
  agent, and budget (`conductor workflow get-execution {id} -c`).

---

## 7. Bootstrapping & prerequisites (`adopt`)

`conductor-code:adopt` is idempotent and does:

1. **Server check** — reachable Conductor at `CONDUCTOR_SERVER_URL` (auto-detect a
   local `conductor server start`), else guide setup (reuses `conductor-setup`).
   *(No LLM provider keys needed on the server.)*
2. **Agent CLIs** — verify the coding-agent CLIs are installed and **authenticated on
   this host**: `claude --version` + login; `codex --version` + login /
   `OPENAI_API_KEY`. At least one is required; both enable cross-agent diversity.
3. **Register defs** — `conductor task create` for each SIMPLE task +
   `conductor workflow create` for each workflow; verify with `conductor task list`.
4. **Start the local worker** — `bootstrap.ts` launches the `TaskManager` poller
   (background) so SIMPLE tasks (incl. `agent`) execute on this machine.
5. **Detect/seed spec framework** — choose OpenSpec/Spec-Kit (default OpenSpec) and
   write `.conductor-code/config.json`; optionally scaffold a constitution.
6. **Seed safety policy** — copy `policy.defaults.json` → `.conductor-code/policy.json`
   (protect `infra/`, CI config, lockfiles; deny destructive commands; budget caps).
7. **Seed agent/model policy** — copy `models.defaults.json` → `.conductor-code/models.json`
   (role → agent + model + effort + price; per-profile overrides) to tune (§2.5).

Prerequisites: a reachable Conductor server, the `conductor` CLI (already installed),
a Node/TS runtime for workers, and **the `claude` and/or `codex` CLIs installed &
authenticated** on the worker host.

---

## 8. Observability, scheduling, resume

- **Observability** — the Orkes/Conductor UI shows the live phase graph, **which
  agent + model ran each phase**, prompts, gate decisions, budget usage, and check
  output; `status` mirrors a summary (and pending gates) in the terminal.
- **Scheduling** — register a Quartz-cron schedule to start `find_bug_workflow`
  nightly for autonomous audits (report + opt-in proposals behind the gate).
- **Resume / recover** — `conductor workflow pause/resume/retry/rerun`; a crashed run
  resumes from the last completed task. Idempotent workers make redelivery safe.

---

## 9. Risks & open decisions

| # | Item | Lean / mitigation |
|---|---|---|
| R1 | **Driving CLI coding agents headless** — `claude -p` **and** `codex exec`: auth, cwd, structured-output capture, cost, concurrency | Confirm each agent's non-interactive + JSON-output contract early; cap concurrent `agent` runs per CLI; pass `cwd=repoPath`; budget per invocation. **Validate both agents in Phase 1.** |
| R2 | Parallel edits conflict | Write-disjoint partitioning + per-group worktrees + an agent-resolved `integrate` per wave; profile-gated (parallel only for full/large). |
| R3 | Gate timeouts for slow human review | Large `responseTimeoutSeconds`; gates releasable from Orkes UI; `status` shows pending gates. |
| R4 | Worker idempotency (Conductor redelivers) | Branch-scoped writes; re-runnable check/git ops. |
| R6 | **CI integration variability** (providers, auth) | `wait_ci` adapts per provider (GitHub Actions first); `integrate` is profile-optional and degrades to "commit only" when no CI. |
| R7 | **Profile mis-selection** (too lean / too heavy) | Deterministic rules + override; default `standard` when unsure; human can bump at GATE 1. |
| R8 | **Guardrail bypass / blast radius** | Pre-flight checks in *every* side-effecting worker; deny-by-default for destructive ops; read-only modes can't write; all edits on a branch. |
| R9 | **Cheap agent/model underperforms** on implementation | Agent/model escalation (Sonnet→Opus, swap Claude↔Codex) before human escalation (§4.6); GATE 2 plan review reduces ambiguity. |
| R10 | **Agent CLI drift** — flags/output formats change across versions | Agent adapter isolates each CLI, pins/records versions, parses defensively, and is integration-tested per agent. |
| R11 | **Parallel integration breaks the build** (cross-group incompatibility) | Contracts pinned in `design` before fan-out; `verify_wave` after every wave; `re_plan` remaining work on failure. |
| R12 | **Planner mis-partitions write-scope** (groups overlap files) | Worktree isolation + agent-resolved merge absorbs residual overlap; `analyze` checks scopes are disjoint before GATE 2. |
| R13 | **Dynamic-fork-of-SUB_WORKFLOW** support varies by Conductor version | Confirm in the spike; SIMPLE-task fallback wrapping the group lifecycle. |
| R14 | **Review loop cost / non-convergence** (builder + reviewer every round) | Per-group budget + round caps; only *blocking* findings keep the loop going (style nits don't); the ladder ends at a HUMAN gate. |
| R15 | **`HUMAN` task richness varies** (OSS Conductor vs Orkes forms/assignment/notifications) | Core release path (`signal-sync` with output) works on both; UI forms/assignment/notifications are an Orkes enhancement; the skill provides the terminal fallback. |
| O1 | Default spec format when none present | **OpenSpec** (configurable). |
| O2 | **Orchestration granularity** — per-task vs per-phase agent invocations | Lean **coarse** (task-level; agent loops internally); revisit if observability/budget control needs finer grain. |
| O3 | Worker language | TypeScript v1; Python alternative if preferred. |
| O4 | `select_profile` — deterministic vs LLM classify | Start deterministic (rules on `kind`+size); revisit. |
| O5 | Default `builder` agent | Start Claude Code · Sonnet; Codex as an alternative/diversity agent, tunable in `models.json`. |
| O6 | Recursive re-decomposition of too-big groups | Deferred; v1 is flat one-level (groups → waves). Add bounded recursion (depth ≤2) later. |

---

## 10. Phased build plan

| Phase | Deliverable | Exit criteria |
|---|---|---|
| **0 — Scaffold + `adopt` + safety baseline** | Skill skeleton, `SKILL.md`, `bootstrap.ts`, taskdef/workflow registration, framework detection, `config.json` + **`policy.json`** + **`models.json` (agent/model policy)** + escalation-gate primitive; **agent-CLI auth checks** | `adopt` registers defs (verified by `task list`), starts a worker, writes config/policy/models, confirms ≥1 agent CLI authenticated; a guardrail violation raises an escalation gate |
| **1 — Agent adapter + `feature` happy path** (validates R1) | **Agent adapter for `claude` and `codex`** (uniform invoke + JSON parse/validate); `feature_workflow` core: `select_profile` + `resolve_models`, both gates, DO_WHILE implement (budget-capped), basic `verify`, `archive` | Spec drafted by architect (Claude·Opus), gated ×2, implemented by builder on a branch, tests pass, archived; **both `claude -p` and `codex exec` proven** to return structured results; kill worker mid-`implement` → **resume** works |
| **2 — Adapter + `fix` + verification depth** | Full spec adapter (both formats, adopt-others'); `fix_workflow` (regression-first); **pluggable `checks.json` suite** + **`provision_env`** worker (race · integration · property + flake-reruns); `verify_workflow` as the **final review vs spec/design** with **bounce-back** | Resume a hand-written change; seeded bug → red → green; `verify` provisions a real env and runs the tiered suite on a *different* agent; a flaky check is rerun, not trusted; a blocking finding bounces back |
| **3 — Parallel implementation + review loop** | Planner DAG (approved at GATE 2); profile-gated wave-loop: `next_wave` → `FORK_JOIN_DYNAMIC` of `implement_group` sub-workflows in worktrees → `integrate` → `verify_wave`; each group runs a **hill-climbing `build↔review` loop** (scored, keep-best) with the **review-driven escalation ladder**; JOIN-time budget aggregation; soft-fail + `re_plan` | A `full` multi-module feature decomposes into write-disjoint groups, runs in parallel waves with per-group review loops, integrates, full suite passes; a forced overlap is merge-resolved; persistent reviewer findings promote the model; a group failure re-plans/escalates |
| **4 — Walking skeleton + system invariants** | The `[skeleton]` phase (thin end-to-end slice before fan-out) + `invariants` in the change model mapped to checks; final-altitude distributed checks (chaos · linearizability · load) on a provisioned env; **graded** bounce-back (invariant failure → re-design) | An architecture-bearing change validates a thin slice vs its invariants on a real cluster *before* any breadth; a violated invariant bounces to design; chaos/linearizability run only at final |
| **5 — Document + examples + Integrate (GitOps)** | Profile-gated `document`/`examples` (scribe); `git_ops` PR + description + issue link + `wait_ci` | A `full`-profile feature run produces docs, examples, and an open PR awaiting green CI |
| **6 — `find-bug` + scheduling** | `find_bug_workflow` (read-only instance of the decompose→fan-out→integrate primitive) + **cross-agent adversarial verify**; nightly schedule | Ranked report from a parallel scan (builder) verified by the other agent (reviewer); opt-in proposals behind the gate; scheduled run triggers |
| **7 — Polish + ship** | `status` UX + budget/agent display, profile + **agent/model-escalation** tuning, `dg` integration, resume/error hardening; package for `conductor-skills` | Dogfooded on a real repo; published to the marketplace |

---

## 11. End-to-end verification

Once the skill exists, dogfood on sample repos:

1. **Adopt/detect + policy + agents** — drop into (a) an `openspec/` repo, (b) a
   `.specify/` repo, (c) an empty repo; confirm detection, def registration, a running
   worker, seeded `policy.json`/`models.json`, and that `claude`/`codex` are
   authenticated (no server LLM keys needed).
2. **Adopt others' specs** — place a hand-written `proposal.md`/`spec.md`; run
   `feature`/`fix`; confirm it resumes mid-stream rather than restarting.
3. **`feature` (full profile)** — spec written in the right format, GATE 1/2 pause and
   release via `signal-sync`, builder agent edits on a branch, broadened `verify`
   passes, **docs + examples produced**, **PR opened** with description + issue link
   awaiting CI, `archive` merges the delta + writes an ADR. Kill the worker
   mid-`implement` and confirm **resume**.
4. **Profiles** — a trivial change skips clarify/design/analyze/docs/examples; a full
   change runs them all.
5. **Safety** — attempt to edit a protected path / print a secret / run a destructive
   command, and force a low iteration or cost cap; confirm the run **pauses at an
   escalation gate** instead of proceeding or overrunning; confirm a read-only mode
   cannot write.
6. **`fix`** — seed a known bug: reproduce → failing regression test → green fix,
   captured as a documented change.
7. **`find-bug`** — confirm a ranked report from a **parallel** multi-area scan
   (`builder` agent) with **cross-agent adversarial verification** (`reviewer` = the
   other agent), proposals only after the gate, and that the **nightly schedule**
   triggers a run.
8. **Multi-agent / multi-model** — inspect the audit: spec/design/plan ran on
   Claude·Opus (max thinking), implementation on the builder agent, `code_review` on a
   *different* agent than built; stall the implement loop and confirm **escalation**
   (bump model and/or swap Claude↔Codex) fires before the human gate; confirm `costUsd`
   reflects per-agent/model rates.
9. **Observability & reproducibility** — every run is fully inspectable in the
   Conductor UI (phases, agent+model ids, prompts, gate decisions, budget, check
   output), and `conductor workflow rerun` replays a past run.
10. **Parallel implementation** — a `full` multi-module feature decomposes (at GATE 2)
    into a write-disjoint group DAG, runs in dependency-ordered **parallel waves**
    (each group a sub-agent in its own worktree), `integrate` merges + `verify_wave`
    stays green; force a file-overlap → confirm agent conflict-resolution; fail a
    group → confirm soft-fail + `re_plan`/escalation; confirm budget is summed at the
    JOIN.
11. **Hill-climbing review loop + HITL** — confirm each sub-agent's loop **scores** each
    round and **keeps the best** (a revision that breaks a passing test is **reverted**,
    not kept); force the reviewer to keep finding issues → confirm a score **plateau**
    climbs the **escalation ladder** (Sonnet→Opus, more budget) and then raises a `HUMAN`
    gate; confirm the **final review bounces a blocking finding back** into a fix pass;
    confirm a `HUMAN` gate both **notifies** and **collects typed input** (an answer, not
    just approve).
12. **Verification depth** — confirm the `checks.json` suite runs **tiered**: cheap
    checks (unit/race) per-round in the group loop, integration/property per-wave, and
    chaos/linearizability/load only at final on a `provision_env`-provisioned cluster;
    confirm a flaky check is **re-run and scored by pass-rate** (not trusted on one
    sample), and a within-noise delta doesn't advance the score.
13. **Walking skeleton + invariants** — for an architecture-bearing change, confirm a
    thin end-to-end **skeleton** is built and validated against declared **invariants**
    on a real env *before* parallel fan-out; force an invariant to fail → confirm it
    **bounces back to design (re-GATE 2)**, not just to implement.
