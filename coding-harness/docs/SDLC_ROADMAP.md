# Full-Fledged SDLC Harness — Design & Roadmap

> **Status: all six build phases (0–6) shipped and live-proven (2026-11-05)** — see each phase's
> RESULT in §6 and the per-phase write-ups (`convergence_spike_result.md`, `team_cycle_result.md`,
> `monitor_console_result.md`, `stages_result.md`, `bootstrap_result.md`, `program_memory_result.md`).
> **Next: closing the gap between the phases and the §4.7 hypotheses** — a 2026-11-06 review of the
> code against H1–H17 found seven hypotheses unbuilt or minimal (H3, H6, H11, H12, H14, H16, H17) and
> four convergence/budget defects (H2, H4, H5, H8). The plan and implementation design to take every
> row to ✅ is [`SDLC_CLOSURE_PLAN.md`](SDLC_CLOSURE_PLAN.md).
> **Closed (2026-11-06): every §4.7 hypothesis H1–H17 and scale-to-30 is now ✅, each with a cited
> Conductor execution — see [`closure_result.md`](closure_result.md).** §7 open questions #3 (feedback
> segmentation: advisory = next tool boundary, blocking = stop, author states urgency), #6 (audit trail:
> `dispatch`/`session` facts + the console item trace) and #7 (stdlib console, bearer-token actor identity)
> are answered there.
> Companion to [`DESIGN.md`](DESIGN.md) (architecture) and
> `workers/IMPROVEMENTS.md` (scale roadmap). This document
> defines how to grow the *coding* harness into a full **software-construction lifecycle**
> harness run by a **collaborating team of agents** — not a one-shot coder.

There are **two dimensions** to the evolution:

- **Stages** (*what* work happens): intake → triage → design → plan → implement → test → review
  → integrate → maintain. Covered in §2–§3, §7.
- **Team execution** (*how* the work gets done): work runs in one of three modes (§4.7 H12) —
  single agent, parallel-independent + best-of-N (today's strength), or, for large/coupled work, a
  **collaborating swarm** of up to hundreds of agents on shared boards, with monitor agents and
  continuous human steering, over runs lasting minutes to days. The swarm is the new centerpiece
  (§4–§6) and supersedes one-shot *for the work that needs it* — not for everything.

---

## 1. Scope — what "full SDLC" means here

For this harness, "SDLC" is the **software-construction lifecycle**: the loop a real team runs to
**build new software, add features, and fix bugs**. It is deliberately *not* the operations
lifecycle.

```
          ┌──────────── codebase knowledge (persistent) ───────────┐
          ▼                                                         │
intake → triage → design → plan/decompose → implement → test/QA → review → integrate
   ▲                                                                           │
   └───────────────── maintenance (bug fix · refactor · deps) ◀───────────────┘
```

**Explicitly out of scope (non-goals), by direction:** deployment / release-to-environment, CD,
canary, rollback, runtime operations, monitoring, observability, incident response, and
CI-pipeline authoring. The harness *verifies* software (runs its tests, reviews it, opens the PR)
and stops at the reviewed branch / pull request. Shipping it is a separate system.

> **"Monitor agents" here are not ops monitoring.** They watch the *construction work in
> progress* — stalls, drift, duplication, quality — and feed back to the team. That is a
> construction-team role (a tech lead / QA lead watching a build), not runtime observability.

**What stays pluggable** (existing strength, preserved): coding **backends** (Claude / Codex /
Gemini, mixable per task) and **spec sources** (local path, git remote, HTTPS bundle). New stages
and roles honor the same provider-agnostic shape.

---

## 2. Current coverage — a strong inner loop, run one-shot

| Stage | Status | Today |
|---|---|---|
| **Intake** | 🟡 per-workflow | `issue_to_pr`, `feature_campaign`/`code_parallel` `instruction`; no single front door |
| **Triage / routing** | 🟡 narrow | `openspec_development` auto-routes parallel-vs-campaign by DAG risk; no request-type classification |
| **Design** | ✅ strong | `design_docs` author↔review loop, campaign design gate |
| **Plan / decompose** | ✅ strong | DAG plan + waves, contracts-first decomposition, `dag_plan_approval` |
| **Implement** | ✅ strong | `code_parallel`, `feature_campaign` — parallel isolated worktrees, multi-backend |
| **Test / QA** | ✅ strong | `test_cycle` (self-fixing, never-fails verdict), `final_verification`, `ui_test` |
| **Review** | ✅ strong | `pr_review`, `local_review`, review sweeps, bounded private investigation |
| **Integrate / merge** | ✅ strong | `merge_worktrees`, `merge_remediation`, `address_pr` |
| **Maintenance** | 🟡 partial | `address_pr`, `*_sweep`; no first-class refactor / dependency / tech-debt path |
| **Greenfield / new project** | ❌ missing | every workflow needs an existing `repoPath`/`repo` |
| **Program / large-system** | ❌ missing | single-level decomposition only; `cc_program` still ⬜ |
| **Codebase knowledge** | ❌ missing | each run re-derives repo understanding from scratch |

**The execution-model gap (the one this revision is about).** Even where a stage is "strong," the
work is done essentially **one-shot**: a subtask is handed to a single agent, which takes its best
attempt; waves fan out independent subtasks but the agents **don't collaborate** — they don't see
each other's progress, don't review each other mid-flight, don't share lessons live, and the team
size is a static `maxParallelism`, not a function of the work. `feature_campaign` has the *bones*
of a team (a DAG plan with `{id, description, dependsOn, files, acceptanceCriteria, checks}`, a
scheduler that runs dependency-ready file-disjoint work, waves, and human checkpoints) but runs it
as a batch of soloists, not a collaborating team.

**Real teams don't work one-shot.** They plan together, split work, review each other, raise
blockers, share what they learned, and a lead re-plans as reality lands — with a PM/QA watching
and a human steering when it matters. That is the model to build.

---

## 3. The five stage gaps (unchanged, now run *on the team substrate*)

These remain the stage-level gaps toward a complete construction lifecycle; each is now executed
by the team model in §4 rather than one-shot.

- **G1 `sdlc_intake`** — a single front door: **classify** a raw request (`feature|bug|refactor|
  chore|greenfield`, size, confidence) → **clarify** low-confidence scope at a bounded gate →
  **normalize** into the downstream artifact (instruction + acceptance criteria / OpenSpec
  proposal / bug repro brief) → **route**. Routing is **two-dimensional**: by *kind* (the stage
  path) **and** by *execution mode*, where — per H12 — the mode is **read off the decomposition**,
  not guessed: one small piece → single agent; independent file-disjoint pieces →
  parallel-independent + best-of-N; coupled pieces beyond one attempt → the collaborating swarm
  (with best-of-N inside each piece). Routing coverable or cleanly-separable work to the swarm is a
  defect. Pure composition over existing engines.
- **G2 `bug_fix`** — reproduction-first: derive a **failing** test, confirm red, localize, fix,
  prove **green**, commit the repro as a regression guard. "Cannot reproduce" is an honest
  outcome, never a silent "fixed."
- **G3 `project_bootstrap`** — description + empty dir → a buildable, **tested** skeleton (with the
  verification command documented so `test_cycle` works from run one) → hand off to the team. The
  on-ramp that fills the "existing repo" precondition every workflow assumes today.
- **G4 `cc_program`** — program design + cross-subsystem contracts → gate → topologically ordered
  milestones, each a team working the shared program branch, independent subsystems in parallel,
  integration check between ranks. The large-new-software unlock (adopts `IMPROVEMENTS.md` §4.1).
- **G5 `repo_memory`** — persistent, on-branch codebase knowledge (module map, invariants, test
  conventions) read natively by later runs; the in-run analogue is the **learning board** in §4.

---

## 4. The team execution model (the swarm)

**Reuse the substrate, but know its limits.** The sibling **`security-harness` already runs a
swarm** for security hunting, and its *generic* substrate — the blackboard fact store, the
`inbox`/`send` message bus, dedup, cross-run learning — ports to construction and should be lifted
into a shared library (see §6), not forked. **But the hardest construction problem does not
exist in the security swarm and does not port:** security findings are *independent and
append-only* (two exploiters never edit the same file; the board just accumulates facts), whereas
construction's central act is **concurrent writes to one shared, mutable codebase**. The
security-specific logic — `swarm_standup` credit-scoring, `oracle_router`, the attempt-lesson
shapes — is rebuilt for construction, not reused. **Write-concurrency over shared code is the
problem this model must actually solve; §4.7 is about why that is hard and what it forces.**

### 4.1 Roles (the team)

All roles are the **same agent worker** under different prompts/tools — not new engines. Which
roles are instantiated, and how many of each, is decided per cycle by the Lead (§4.3), scaling the
team from **1 agent** (a trivial change) to **hundreds** (a kernel-scale rewrite).

| Role | Does | Built from |
|---|---|---|
| **Lead / Coordinator** | Owns the tracking board; re-plans, re-prioritizes, assigns ready work, sizes the next wave | `feature_campaign` planner + `swarm_standup` |
| **Architect** | Design + cross-component contracts | `design_docs` |
| **Implementer** (the bulk; 1→100s) | Takes a work item, codes in an isolated worktree, reads the board before/while working, posts progress + lessons | `code_parallel`/`campaign_subtask` + `swarm_read_board` |
| **Reviewer** | Peer-reviews teammates' output *mid-flight*, posts findings to the review board | `code_review`/`pr_review` |
| **Tester / Verifier** | Runs `test_cycle`, posts the verdict | `test_cycle` (unchanged) |
| **Integrator** | Merges worktrees, resolves conflicts | `merge_worktrees`/`merge_remediation` |
| **Monitor** (new) | Watches the boards continuously; detects stall / drift / duplication / quality regression / budget overrun; injects directives or escalates to a human | new role over the same agent worker |
| **Human** | A first-class actor, not an external gate — drops in any time to read a rollup and post feedback ("do this", "don't do that", "change that") against an item, an agent, or the team, through the *same* boards and message bus agents use. Carries **override authority** (§4.4) | the existing people, via TUI / console, writing to the boards |

### 4.2 Boards (durable shared state — the thing that makes it a team)

Three boards hold the team's shared, mutable state. At swarm scale (hundreds of agents, days) this
**cannot** live in workflow variables — it is a **persistent blackboard** (the security-harness
`common/blackboard.py` fact store; back it with the harness's Postgres backend for scale). Thin
worker tasks read/write it; Conductor orchestrates *around* it.

1. **Tracking board (Jira-like).** The living backlog of work items —
   `{id, title, kind, state: backlog|ready|in-progress|review|blocked|done, assignee, dependsOn,
   files, acceptanceCriteria, checks, priority, attempts, cost}`. This is `feature_campaign`'s
   static DAG **evolved into a board the Lead mutates** every cycle: splitting items, re-opening
   failed ones, re-prioritizing, blocking/unblocking. It is what a human watches and drives.
2. **Learning board.** Accumulated lessons, invariants, gotchas, and "what was already tried + why
   it failed" — the security-harness **`attempt_lesson`** + facts. Shared **live within a run** (a
   teammate's lesson this cycle steers the next agent) and **across runs** (prior runs' lessons
   seed planning — the cross-run self-learning already in `swarm_rehypothesize_prep`). This is the
   in-run face of **G5 `repo_memory`**.
3. **Review / feedback board.** Review findings, feedback threads, and **directives** addressed to
   specific agents or the whole team — the `swarm_inbox`/`swarm_send` message bus plus
   `focus_directive` steering. This is where reviewers, monitors, **and humans** inject feedback.

**Mechanics — where it's stored, and how actors communicate.** There are **three** stores, and
mixing them is a defect:
- **Conductor workflow variables** — orchestration/control state only (counters, flags, scores,
  session ids). Per-run, in Conductor.
- **The git branch / worktree** — the code itself and large design/contract docs (artifacts-on-
  branch, `IMPROVEMENTS.md` §3.1); agents read these natively from disk.
- **The blackboard** — all *shared team state and communication*: work items, lessons, messages,
  findings, and **all feedback** (directives, constraints). This is the security-harness
  `common/blackboard.py`, backed by a local dir today and **Postgres/S3 at scale** (H6).

The blackboard is an **append-only log**: every record is a unique key
`facts/{runId}/{kind}/{author}__{uuid}.json`, so **no two actors ever write the same key — no locks,
no lost updates, even with hundreds of concurrent writers**, and shared state is never a mutable
object to contend on. Readers **fold the log** (`assemble_state` / `campaign_rollup`) into the
kanban, the lesson set, and the H7 rollup. The log *is* the audit trail (§7.6).

- **Feedback is a record on the blackboard.** A human/peer/monitor directive is an immutable
  appended fact (a `directive`, or a `constraint` on an item id); its `author` field is *who said it*
  = its authority (H16). A sticky constraint persists until a later appended record supersedes it.
- **Communication is append + fold.** Agent→board: `post_fact`. Agent→agent / human→agent:
  `send_message(to = workflowId | role | broadcast)` → the recipient's `read_inbox`. The human is
  just another `author`, so human feedback travels the exact same wire as a peer's (§4.1).
- **Agents *take* feedback at two points (H11).** Default/durable: a running `@worker_task` is opaque
  while executing, so between short segments a thin `read_board` step gathers the agent's inbox +
  its item's constraints and passes them into the next segment's **input**, which injects them
  (SDK `UserPromptSubmit` hook) and **resumes the session** (`resumeSessionId`). The workflow
  *pulls*; segment length = feedback latency (§7.3). Blocking-only: a live streaming session takes
  an `interrupt()`. Feedback with no agent on the item waits as a constraint for whoever picks it up;
  the standup also folds all new feedback into re-planning each cycle.

### 4.3 The cycle (how it runs, minutes to days)

The team runs a durable **coordinator loop** (a `DO_WHILE` over the boards — no execution
deadline, per `AGENTS.md`), repeating until the tracking board drains or a human stops it:

```
standup ──▶ size & fan out ──▶ implement ∥ review ∥ monitor ──▶ integrate ──▶ learn ──▶ re-plan
   ▲                                                                                      │
   └──────────────────────────────── (board not empty) ◀──────────────────────────────────┘
          ▲ human steering & monitor directives enter here, asynchronously, every cycle
```

1. **Standup** (Lead) — read the tracking board, score ready work by priority/credit/complexity,
   and **decide the team size for this wave** (1 to hundreds) from the ready-work count, the
   dependency structure, and the budget. (= `swarm_standup` emitting a dynamic fork of width N.)
2. **Fan out** — `FORK_JOIN_DYNAMIC` over the selected work items. Independent, file-disjoint items
   run in parallel; dependent items serialize across cycles. At program scale this nests
   (program → feature teams → sub-teams) with bounded recursion depth.
3. **Implement ∥ review ∥ monitor** (concurrent) — implementers code and **post progress + lessons
   to the boards as they go**, reading peers' board state before and during work so they build on,
   not collide with, each other (= `swarm_read_board`/`_peer_digest`). Reviewers peer-review
   completed items **mid-run**, not only at the end. Monitors watch in parallel (§4.5).
4. **Integrate** — merge ready worktrees; `merge_remediation` on conflict. Failures re-open their
   items on the board with the failure detail attached.
5. **Learn** — reconcile the cycle's lessons/findings onto the learning board (= `swarm_reconcile`)
   so the next standup plans with them.
6. **Re-plan** — the Lead folds in test results, review findings, monitor directives, and **any
   human steering** (§4.4), updates the board, and loops.

**Durability.** Conductor makes each cycle durable: the run survives restarts, resumes
mid-flight, and has no wall-clock deadline — so a run can legitimately last days. The boards
persist independently of any single task, so a human can inspect and steer across that whole
span.

### 4.4 Feedback loops & human steering

Four loops run continuously, not just at fixed gates:

- **Peer-review loop** — implementer → reviewer → revise, *mid-run*.
- **Learning loop** — lessons posted → read by peers this cycle and by the next standup → fewer
  repeated mistakes; compounds across runs via `repo_memory`.
- **Monitor loop** — monitors → directives on the review board / escalation → Lead re-plans (§4.5).
- **Human-steering loop** — the key addition for long runs, modeled as **the human being just
  another actor** (§4.1). The human drops in any time, reads a rollup (§4.5), and posts feedback
  through the *same* channels agents use — a directive to a specific agent or the team (message
  bus / `focus_directive`), a changed constraint or acceptance criterion on a tracking-board item,
  or a change request on the review board. No separate "human transport": a human "don't do X" and
  a peer's "don't do X" travel the same wire. This is *in addition to* the fixed checkpoints
  (`feature_campaign`'s design / plan / wave / final WAIT gates), which remain for the few decisions
  that must block. Continuous steering is what makes a multi-day run governable without babysitting
  a gate.

**The human is an actor with override authority, and feedback must reach work already in flight.**
Two things keep "just another actor" from meaning "an equal vote that can be ignored or lost":

- **Authority & stickiness.** Human feedback **outranks** agent and monitor directives, and
  **persists as a constraint on the item** (not a one-off message that evaporates at the next
  cycle). "Don't use library X" stays true for every actor who later touches that item, until a
  human lifts it. An agent that cannot satisfy a human constraint **blocks and escalates** rather
  than silently overriding it.
- **In-flight incorporation (how an agent "adjusts course").** An agent mid-task must take new
  feedback, not only the next one to pick the item up. Two mechanisms, chosen by urgency (grounded
  in the Agent SDK, [`docs/CLAUDE_AGENT_SDK.md`](CLAUDE_AGENT_SDK.md)):
  - *Advisory* ("also do…", "prefer…") → **turn-boundary checkpoint**: the agent works in bounded
    turn segments; between segments a step reads the board/inbox and, if there is new feedback,
    **resumes the session with it injected** (session resume + `UserPromptSubmit` context
    injection). Durable (each segment is a completed Conductor task) and simple; latency = one
    segment. This is the **default**.
  - *Blocking* ("stop", "that's wrong, change direction now") → **live steering**: the worker holds
    a streaming-input session and **queues the message or `interrupt()`s** it mid-turn. Fine-grained
    and immediate, but a long-lived subprocess (~1 GiB each — see H6) that is opaque to Conductor
    while running, so it is reserved for the urgent blocking case, not the common one.
  - Feedback that arrives when **no agent is on the item** simply lives on the board as a sticky
    constraint and is honored by whoever picks it up — the async, days-long case needs nothing
    extra. If the work has already moved past what the feedback targets, the item is **re-opened**
    with the feedback attached, never silently dropped (H2's anti-thrash cap still applies).

### 4.5 Monitor agents

Monitors run **concurrently** with implementation (a parallel branch of the cycle, or a
periodically re-dispatched task) and have **bounded authority** — they nudge and escalate, they
don't silently rewrite the plan:

- **Watch:** the tracking board (stalled / high-attempt items), the learning board (repeated
  failures, flaky tests), the review board (findings piling up), and budget/scope vs. plan.
- **Detect:** a stall (in-progress too long / too many attempts), **drift** (work diverging from
  its `acceptanceCriteria` or the design), **duplication** (two agents overlapping on the same
  files/intent), quality regression, and budget/scope overrun.
- **Act:** post a `focus_directive` to the implicated agent/team, re-open or re-prioritize an item,
  or **escalate to a human** for anything above its authority. Big decisions go to people; small
  corrections stay in the loop.

### 4.6 Why the added complexity is justified (per `AGENTS.md`)

The simplicity policy demands naming the requirement that the one-shot model cannot meet: a
**collaborating team that scales 1→hundreds and runs minutes→days with live feedback and human
steering** is not expressible as independent one-shot subtasks. Complexity is contained by: **one**
shared-state abstraction (the boards), **one** coordinator loop, roles as prompts over the
**existing** agent worker, and **reuse of the proven `security-harness` swarm substrate** instead
of net-new machinery. Each board and role must still name the loop it closes.

---

### 4.7 The hard problems this model must solve (adversarial pass)

An honest list of where this design can fail, and the response each failure forces. These are not
"nice to have later" — several are go/no-go prerequisites, and they reshape the roadmap (§6).

**H1 — Write-concurrency over shared code (the central problem).** The whole model rests on agents
in isolated worktrees whose work is merged. File-disjoint partitioning — what the campaign
scheduler does today — collapses the moment two items touch shared code (a router, a schema, a
types file, an API contract), which at scale is *most* non-trivial work. N long-lived branches
merged pairwise is exactly the exponential merge pain real teams avoid with trunk-based
development. **Response:** the team must integrate like a real team, not a merge farm —
**contracts/interfaces frozen first** (H4), **short-lived worktrees with frequent re-sync to the
shared branch** (not long-lived forks), ownership of shared files recorded on the tracking board so
two agents never hold the same file open, and an **integrator that rebases/re-syncs continuously**
rather than a big-bang merge at wave end. If this cannot be made to work, the swarm premise fails
for anything but file-disjoint work — so it is the **first spike**, before building the cycle.

**H2 — Convergence is not guaranteed.** Agents *add* work (split items, file bugs, break each
other), so the board can grow faster than it drains, and one merge can invalidate many in-flight
items → rework cascades and oscillation (A changes an interface, B breaks, B's fix breaks C).
**Response:** an explicit **convergence/termination model** — contract freeze (H4) to stop
interface churn, an anti-thrash damper (cap re-opens per item; quarantine an item that oscillates
for human/lead attention), a **monotonic "done" definition** (system-level acceptance green, not
just per-item), and a cycle budget. Without this the run never ends or never stabilizes.

**H3 — Flat Lead/board caps real scale far below "hundreds."** One Lead reading the whole board
each cycle is a context-window bottleneck; thousands of items do not fit one agent. **Response:
hierarchy is core, not a late `cc_program` feature.** Teams-of-teams, each with a *local* board
and a sub-lead; a top Lead sees only rolled-up team status, not every item. The flat single-board
model is correct only up to ~one feature; beyond that it must nest. Pull this forward into the core
cycle design.

**H4 — Parallel work converges only behind frozen contracts.** The one mechanism that lets real
teams build in parallel is agreeing on interfaces first and not changing them under each other.
The doc mentions contracts in passing; it must be a **first-class, enforced phase**: the Architect
produces cross-component contracts, a gate freezes them, and a contract change becomes a
**controlled event** (its own board item that re-plans dependents), never an ambient edit. This is
the precondition for H1 and H2.

**H5 — Economics / budget is a go/no-go, not a monitor feature.** Hundreds of agents over days,
each with a reviewer, multiplies cost by orders of magnitude, and per-run budget enforcement
(`IMPROVEMENTS.md` §3.3) is **still ⬜**. **Response:** real-cost pricing and a **hard budget gate
are a prerequisite** (Phase 0/1 blocker) — a swarm that cannot stop itself on spend must not be
startable. Do **not** claim a precise up-front estimate ("this scope ≈ $X") — rework depth is
unknowable in advance and the number would be false precision; instead gate on a **ceiling** plus
**burn-rate monitoring with early-stop** (alert the human as spend approaches the ceiling, and halt
at it), so the human approves an *order of magnitude* and the run proves or disproves it as it goes.

**H6 — Physical substrate.** "Hundreds of agents" means hundreds of concurrent coding-agent
processes → hundreds of worker slots / machines (worker-fleet health is already a recurring pain at
small scale), Conductor payload limits (already hit — artifacts live on the branch, not in vars),
`FORK_JOIN_DYNAMIC` width limits, and a blackboard under hundreds of concurrent writers (the
current sqlite `c123.db` is already ~267 MB). **Response:** Postgres-backed board from the start
for any real swarm; a concrete fleet-provisioning story (local caps out fast — this implies cloud
workers); and explicit width/throughput ceilings with graceful queueing, not an assumed infinite
fork.

**H7 — Human comprehension at scale.** A human cannot follow thousands of board events over days,
so "read the board and steer" is not an interface. **Response:** the **monitor's primary output is
a continuously-maintained, human-digestible rollup + an exception queue** (what needs a decision),
and steering is done against that summary. The human drives by exception, not by watching.

**H8 — Peer review has no oracle and can become theater.** Security review has ground truth (the
exploit fires or it doesn't); construction review's only oracle is tests. N same-model agents
reviewing each other risk rubber-stamping or looping while doubling cost. **Response:** peer review
must earn its place by catching what tests cannot (contract/design violations, acceptance-criteria
drift, security/perf smells) — scoped to that, bounded in rounds, and never a substitute for
`test_cycle` as the correctness oracle. If a review round adds no actionable change, it is dropped,
not repeated.

**H9 — Graceful degradation.** The board/standup/monitor machinery is pure overhead for a trivial
one-agent change. **Response:** the model must degrade to *literally one agent, no boards* below a
size threshold (that is `code_parallel`'s job), and `sdlc_intake` must route there. Ceremony scales
with the work; the floor is zero ceremony.

**H10 — Stale learning misleads.** Cross-run lessons and `repo_memory` can be wrong after the code
moves on, and a confidently-wrong lesson actively degrades planning. **Response:** lessons carry
provenance + confidence and **decay/are invalidated** when the code they reference changes;
anything a lesson names (a file, a symbol) is verified to still exist before it is trusted.

**H11 — Feedback latency vs. durability (the "adjust course now" tradeoff).** Letting a human (or
peer) redirect an agent *mid-task* fights the durable one-shot worker model: the SDK's live
steering (`interrupt()`, queued messages) needs a **long-lived streaming session** — a ~1 GiB
subprocess that is opaque to Conductor and loses in-flight state if the worker dies — while the
durable path (bounded segments + session resume) only incorporates feedback at **segment
boundaries**, so "now" is really "within one segment." **Response:** default to the durable
turn-boundary path and keep segments short enough that advisory feedback feels responsive; reserve
live steering for genuinely blocking "stop/change direction" feedback, and treat a dead streaming
session as a resumable item, not lost work. Do not make every agent a long-lived streaming session
by default — H6's substrate cost makes that untenable at scale.

**H12 — Collaboration vs. independent sampling: a resolved boundary, not a contest.** The attack:
the harness *already* does parallel-**independent** sampling + pick/merge-best (`keep_best`, the
ladder) with **none** of H1/H2/H15 (no merge collisions, no thrash, no shared bias) — so why
collaborate at all? Resolved in three parts:

- **(a) They do not compete — best-of-N is a tactic *inside* the swarm, not an alternative to it.**
  The collaborating swarm is the *outer structure* (decompose → freeze contracts → integrate
  continuously → share lessons); each individual subtask *within* it is still executed by **N
  independent attempts, pick-best**. The swarm does not replace best-of-N — it lets best-of-N apply
  to work too large for a single best-of-N pass. "Just use best-of-N" is not a rival design; it is a
  *component* of this one.
- **(b) The boundary is structural and decidable, not a judgment call.** Best-of-N on the *whole*
  task has two hard preconditions — the task must **fit one attempt's scope**, and success must be
  **verifiable to pick a winner**. When the first fails, best-of-N is not worse, it is *incapable*:
  too big for one attempt → no independent attempt ever completes the whole, so there is **no "best"
  to pick** → you must decompose; and if the decomposition is **coupled** (B needs A's
  interface/merged result), independently-sampled parts are sampled against *guessed* interfaces and
  **do not compose** → the parts must share a frozen contract and build on each other's merged
  output, which *is* collaboration. So in the swarm's regime, best-of-N is **structurally
  disqualified**, not merely inferior.
- **(c) The mode is an output of decomposition (H14), not a blind router guess.** Always attempt a
  decomposition first; its *shape is the routing decision* — one small piece → single agent; several
  **independent** file-disjoint pieces → parallel-independent + best-of-N per piece, trivial merge;
  **coupled** pieces beyond one attempt → the collaborating swarm, with best-of-N inside each piece.

**Residual (honest).** This makes the swarm's regime *precise*, and possibly *narrow* — much real
work is single-attempt-coverable or cleanly separable, and must route to the cheaper modes (routing
such work to the swarm is a defect). That is correct, not a weakness: *within* its regime (large +
coupled) the swarm is **uncontested**, because best-of-N cannot run there. The only open *empirical*
question (Gate 0) is therefore not "does the swarm beat best-of-N" but "is the boundary drawn
correctly — does swarm-routed work genuinely resist best-of-N, and does the swarm succeed where
best-of-N structurally can't."

**H13 — The correctness oracle is gameable.** If items are marked done on "tests green" and
`test_cycle` is the sole arbiter, agents are incentivized to write tautological or teach-to-the-test
cases to clear the gate — green ≠ correct, and at swarm scale this reward-hacking compounds.
**Response:** tests cannot be *both* authored by the implementer *and* the sole done-gate for the
same item — separate the author from the oracle (independent test authorship / review of the tests
themselves against the acceptance criteria), and keep human/acceptance spot-checks on a sample.
This is why H8's peer review earns its place: catching a gamed oracle is exactly what tests can't.

**H14 — Decomposition is the bottleneck, and it is under-designed.** The swarm's entire value rides
on the Lead cutting clean, low-coupling, well-contracted items; bad boundaries produce thrash no
matter how good execution is. This doc specifies *execution* (boards, monitors, feedback) in depth
and planning as "the Lead does standup" — inverted priorities. **Response:** decomposition quality
is a first-class, measured concern (coupling/overlap of the proposed items, contract completeness)
with its own review gate, and a bad decomposition is re-planned before any agent is spawned, not
discovered through failed merges.

**H15 — Shared memory amplifies shared error.** One wrong lesson on the learning board ("auth uses
JWT" when it is sessions) is read by every agent → *correlated* failure, faster than isolated agents
would err. The security swarm filters false facts with an oracle; construction lessons are often
unverifiable opinion. **Response:** lessons carry provenance + confidence (H10), unverified opinion
is marked as such and never promoted to a constraint, and a lesson contradicted by a later verified
outcome is retracted swarm-wide. Prefer verifiable facts (a contract, a passing test, a file that
exists) over asserted beliefs on the board.

**H16 — "Human outranks" is an unsafe blank check.** *Which* human? Multiple humans conflict; a
human directive can be wrong or go stale; "sticky until lifted" lets a day-1 constraint
over-constrain day-3 into deadlock while agents obey blindly. **Response:** authored-by identity on
every directive (as the security swarm trusts only a configured identity); a **constraint lifecycle**
(expiry/review, conflict detection across sticky constraints, an over-constrained board flagged to a
human rather than deadlocking); and **agent pushback** — an agent that judges a human constraint
harmful raises it ("this will break X") instead of silently complying or silently overriding. Human
authority is high, not absolute-and-unexaminable.

**H17 — Durability and sandboxing gaps in the substrate.** Conductor makes *orchestration* durable,
but an agent's **uncommitted worktree edits** live on a worker's local disk — a dead worker loses
them, so "each cycle is durable" (§4.3) is only true for *committed* state. And hundreds of agents
**executing agent-written code and tests** is running untrusted code at scale with no sandbox named.
**Response:** commit-or-lose discipline (frequent commits / snapshots so a lost worker costs one
segment, not a day) and an explicit sandbox for agent-executed code — reuse the `security-harness`
posture here rather than inventing one.

**Scale is aspirational until proven.** "1→hundreds / minutes→days" is the target, but the
demonstrated ceiling today is ~4–8 parallel groups over ~3 waves. Treat "hundreds" as unvalidated:
the roadmap must **prove scale incrementally** (10, then 30, then higher), and every scale claim in
this doc is a hypothesis to test, not a capability to assume.

**Net:** the open *premise* risks are **H1, H2, H4, H5, H13** — if any cannot be answered, the swarm
model is wrong for that class of work, and the system falls back by design to single-agent or
parallel-independent + best-of-N (which the harness already has). **H12 is resolved:** best-of-N is
a tactic *inside* the swarm, and the swarm's regime — large + coupled work — is where best-of-N is
structurally disqualified, so there is no contest to lose; the only open question there is whether
the routing boundary (an output of decomposition, H14) is drawn correctly, which Gate 0 checks. H1
and H4 are the first spikes; H5 gates any real run; H13 and H14 decide whether the output is
trustworthy.

### 4.8 The human interface — consoles over the boards

None of §4.1's human-actor model, §4.4's steering, or §4.5's rollup is real without a UI. The
design principle keeps it from becoming a second system:

**The boards are the single source of truth; every UI is a view + editor over them.** A UI holds no
state of its own. A human action in any UI is a **board write** — the same write a monitor or agent
makes (§4.1). This means there can be several surfaces at once (TUI, web console, notifications, even
the Conductor execution view) with no sync problem, and "the human is just another actor" is
literally true: the UI is only their window onto the shared boards.

**Two surfaces, one backend (reuse what exists):**
- **TUI** — extend the existing Textual TUI (`tui/` already has `dashboard`, `approvals`,
  `run_detail`, `sessions`, `chat`, `notify`). CLI-native, fast, for starting and driving a run.
  Today it is Conductor-execution-aware; make it **board-aware**.
- **Web Swarm Console** — a days-long, hundreds-of-agents, possibly multi-viewer run needs an
  always-on, shareable, visual surface a terminal can't give (a kanban board, a cost-burn chart, a
  team tree). This is the primary monitoring surface for a real swarm; it reads the board store
  (Postgres, H6) and Conductor APIs directly.

**The views a human needs** (each a board projection):
1. **Run overview** — health, phase, mode (§4.7 H12), team size, progress, **cost burn vs. ceiling**
   (H5), test/review state at a glance.
2. **Rollup + exception queue** — the **default landing surface**, not the raw firehose. This is the
   answer to H7: the monitor-maintained summary plus the list of things that actually need a human
   decision. A human drives from here.
3. **Tracking board** — the Jira-like kanban (items by state, assignee, blockers); drill into any
   item to see/edit its constraints and acceptance criteria.
4. **Agent view** — what a given agent is doing now, its live session/transcript, its worktree diff.
5. **Learning & review boards** — lessons (with provenance/confidence, H15) and review threads.
6. **The artifact** — the actual branch diff, test results, the thing being built.

**Steering is exposed everywhere as "act as an actor"** — from any view a human can post a directive
to an agent/team, edit a constraint or acceptance criterion on an item, re-prioritize, inject or
kill work, answer a gate, or pause/stop the run. Advisory vs. blocking maps to H11's two paths.

**Async & scale.** For multi-day runs the human does not sit and watch: **push notifications**
(reuse the TUI's `notify`) ping them only when the exception queue gains a decision they must make,
and the console lets them catch up on what changed while away. At large team sizes the UI **must not
render the firehose** — it aggregates (team-level rollups, drill-down on demand) per H3/H7; showing
500 live agents is a non-goal.

**Honest scope.** A polished web console is real, phased work, not a free side effect. Minimal
board-viewer + steering in the TUI comes with the Phase-2 team cycle (so a run can be watched at
all); the full web console, rollup/exception surface, and notifications land with Phase 3 (monitors
& steer-by-exception), which is where they first have something to show.

## 5. Target architecture — swarm substrate under the stages

```
                  ┌──────── boards: tracking · learning · review ────────┐  ← humans steer here
                  │               (durable blackboard)                    │     (async, any time)
                  ▼                                                       │
 raw request → sdlc_intake (G1) ──route──┐                               │
   greenfield → project_bootstrap (G3) ──┤                               │
   bug        → bug_fix (G2) ────────────┤   ┌─────── team cycle (§4.3) ──┴──────┐
   feature    → ───────────────────────────▶ │ standup → fan out → implement ∥   │
   large/multi→ cc_program (G4) ──────────┤   │ review ∥ monitor → integrate →    │
   spec-driven→ openspec_development ─────┘   │ learn → re-plan   (DO_WHILE)       │
                                              └───────────────┬───────────────────┘
                                                              ▼
                                                    reviewed branch / PR
```

The stage workflows (§3) decide *what* a run is; the **decomposition** then decides *how* (H12):
**single agent** (one small piece — `code_parallel`, no boards), **parallel-independent +
best-of-N** (independent pieces — the existing `keep_best`/ladder strength, no collaboration
overhead), or the **collaborating team cycle** (§4.3 — coupled pieces beyond one attempt). The modes
**compose**: best-of-N is the per-subtask execution tactic, the swarm the outer decompose/integrate
structure that lets best-of-N reach work too large for a single pass. `feature_campaign` is the seed
to evolve into that third engine; the swarm is the regime for large/coupled work, not the default.

**Principles carried forward:** simplicity-justified additions; one workflow version; no JQ, no
timeouts; verification embedded via `test_cycle` with honest outcomes; backend/source-agnostic.

---

## 6. Roadmap

Each phase is independently shippable, leaves the harness strictly more capable, and ends with a
**real Conductor execution** as proof (per `AGENTS.md` and the "run the full harness" rule).

### Phase 0 — Substrate + budget (prerequisites)
- Lift **only the generic substrate** into a shared library both harnesses use: the blackboard
  fact store, the `inbox`/`send` message bus, dedup, and the cross-run-learning plumbing. The
  security-specific logic (`swarm_standup` scoring, `oracle_router`, attempt-lesson *shapes*) stays
  in `security-harness`; construction rebuilds its own (H1 reuse caveat).
- Land **real-cost pricing + a hard per-run budget gate** (`IMPROVEMENTS.md` §3.3) — H5 says a
  swarm that cannot stop itself on spend must not be startable.
- *Acceptance:* both harnesses import the shared substrate; security-harness swarm runs unchanged;
  a run aborts cleanly at a configured budget ceiling with a truthful cost report.

### Phase 1 — Prove the premise (H1 + H4 spike) — GO/NO-GO
- On a small task **deliberately chosen to touch shared code**, prove the integration model:
  **contract freeze** first (H4), short-lived worktrees with **continuous re-sync** to a shared
  branch, shared-file **ownership on the board**, and an integrator that rebases continuously (H1).
- *Why before the cycle:* if parallel agents cannot converge on shared code, the swarm premise is
  wrong beyond file-disjoint work and the honest outcome is to scope it there. Do not build Phase 2
  until this passes.
- *Acceptance:* ≥3 agents concurrently modify overlapping code and converge to a green, reviewed
  branch without a big-bang merge — or a documented finding that they cannot, and why.
- **RESULT — GO (2026-11-05).** Shipped as `convergence_spike` v1 (workers in `campaign/tasks.py`
  over `common/{contract_freeze,file_ownership,continuous_integrate}.py`). On a forced-overlap task
  where three items all edit one shared `registry.py`, the spike converged **3/3 forks onto one green
  shared branch by continuous rebase, zero big-bang merge, zero reopens** — real Conductor execution
  `93ccaa36-7177-4bcb-949c-4b6b94b3a447`, independently re-verified (linear history, coherent shared
  file, frozen contract untouched, frozen tests pass). The file-disjoint baseline (`code_parallel`,
  execution `2f6558d6-…`) could not even represent the overlap — it auto-repaired the plan to strip
  the shared file into a lone subtask and ended `implementation_unavailable` with a big-bang-merge
  conflict. Full write-up: [`convergence_spike_result.md`](convergence_spike_result.md).
- **Hardened (2026-11-05): both wiring gaps closed.** `convergence_spike` is now an ownership-gated
  **wave loop**: ownership **gates dispatch** (clean run `31fba11b` serialized the all-contended task
  into 3 waves of one agent each and still reached GO, 3/3), and each wave records real agent spend so
  the aggregate budget gate's **halt path is live** (clean run `f40c0393`, ceiling `$0.01`: halted after
  `$0.1177`, leaving 2 items undispatched → honest NO-GO naming the mechanism). The integrator also now
  detects **empty/failed deliveries** (never mis-counting a no-op fork as integrated) and **re-queues
  transient failures across waves** up to an attempt cap — live-proven by run `28b232cf` (a no-op fork
  retried then terminally reopened → honest NO-GO; ownership gating ran the two disjoint items
  concurrently and serialized only the contended one). **Phase 2 is unblocked.**

### Phase 2 — Team cycle for construction (evolve `feature_campaign`)
- Replace the static DAG with the **tracking board** (**hierarchy-ready** from the start, H3: a
  board can hold sub-boards); add the **learning** and **review** boards; turn the wave scheduler
  into the **standup → fan out → implement ∥ review → integrate → learn → re-plan** cycle with
  **dynamic team sizing**, **mid-run peer review** (bounded, oracle-aware, H8), and **live lesson
  sharing**. Keep the fixed checkpoints. Enforce **graceful degradation** (H9): below a size
  threshold `sdlc_intake` routes to plain `code_parallel` — no boards, no ceremony.
- Make the existing **TUI board-aware** (§4.8) — the tracking board, agent view, and basic steering
  — so a run can actually be watched and nudged while it runs.
- *Acceptance:* a multi-item feature where agents demonstrably read each other's board state and
  lessons, a peer review catches an issue mid-run, a trivial task runs with zero board overhead, a
  human watches the board live and nudges one item, and the run produces a reviewed branch at a team
  size chosen from the work.
- **RESULT — DONE (2026-11-05).** Shipped as `team_build` v1 (`workers/team/tasks.py` +
  `workers/common/team_board.py` — the construction read model `blackboard.py` had deferred to Phase 2
  — over the Phase-1 primitives). The static DAG is now a **mutable tracking board** with **learning**
  and **review** boards (kinds on the one append-only log); the wave scheduler is the
  **standup → fan out → implement ∥ review → integrate → learn → re-plan** DO_WHILE with **dynamic team
  sizing** (`team_board.size_wave`), **mid-run peer review** (a read-only `coding_agent` reviewer, a
  *different* agent than the implementer — H8/H13), and **live lesson sharing**. The fixed checkpoints
  and ownership-gated dispatch carry over. **Graceful degradation (H9)** is live: `team_route` sends a
  single small item to plain `code_parallel` with zero board overhead. The **TUI is board-aware**
  (`tui/board.py`, `tui/screens/board.py`): a live board viewer + steering, every action an authored
  board write. Proven by real Conductor executions: `team_build` `2501ed93-4aeb-4a69-87d2-a589a436d995`
  delivered a 3-item feature on overlapping shared code (3 ownership-serialized waves, each peer-
  reviewed) to a green reviewed branch — **DELIVERED**, zero big-bang merge, linear history, frozen
  contract intact; wave 2's prompt carried wave 1's lesson (live lesson sharing) and a **human
  directive posted mid-run reached the `sms` standup** (live human steering). Single-item run
  `11d5e604-58e2-4553-b8f0-af63ca2e6294` routed to `code_parallel` with **zero blackboard facts**
  (graceful degradation). The full **peer-review `changes → reopen → revise → green` loop** is proven
  live (`cf79c731`): a real reviewer returned `changes` on a **hidden `reviewCriteria`** (the reviewer's
  oracle, not shown to the implementer — H8/H13), the item reopened, the re-dispatched agent made the
  fix, review passed, DELIVERED. **Scale** past the demonstrated 3–4 is proven live too: an **8-wide
  single wave** (`c11cf2a5`) converged DELIVERED 8/8, green. Full write-up:
  [`team_cycle_result.md`](team_cycle_result.md). **Phase 3 is unblocked.**

### Phase 3 — Monitors & steer-by-exception
- Add the **monitor** role (stall/drift/duplication/quality/budget detection → directives /
  escalation). Per H7, the monitor's primary output is a **continuously-maintained human rollup +
  an exception queue**; humans **steer by exception** against that summary (re-prioritize,
  inject/kill work, post directives), not by watching raw board events. Ship the **web Swarm
  Console** (§4.8) — rollup + exception queue, kanban, cost-burn, agent drill-down — plus **push
  notifications** so a days-long run pings the human only when a decision is needed.
- *Acceptance:* a long run where a monitor catches drift and corrects it without a human; a human,
  notified asynchronously, drives a decision from the exception queue in the console (not the raw
  board) and the next standup honors it.
- **RESULT — DONE (2026-11-05).** Shipped the **monitor** role (`workers/common/monitor.py` +
  `team_monitor`, wired into every `team_build` wave) with bounded authority (open question #2):
  stall/quality → autonomous nudge+re-prioritize; blocked/duplication/budget-breach → the **exception
  queue** (new `team_board` `exception` kind, H7). Shipped the **web Swarm Console**
  (`tui/console.py`, `make console`) — a stdlib view+editor over the boards: rollup + exception queue
  (the landing surface) + kanban + **cost-burn** + agent drill-down + steering POSTs — and **push
  notifications** (reuse the TUI notifier; fire only when open exceptions rise). Proven by real
  execution `120f5c7b-daeb-4b2d-be91-a53d4537b8fb`: a thrashing no-op item was **corrected
  autonomously by the monitor** (directive + priority→102 at the stall threshold, no human) and then
  **escalated** when it blocked; a **human then resolved the exception from the console**
  (`POST /api/steer` → open exceptions 1→0). The console was also verified rendering the delivered
  Phase-2 run's board + cost-burn. Full write-up:
  [`monitor_console_result.md`](monitor_console_result.md). **Phase 4 is unblocked.**

### Phase 4 — Stage front door & bug depth
- **G1 `sdlc_intake`** (also the graceful-degradation router, H9) and **G2 `bug_fix`**.
- *Acceptance:* a raw feature request and a raw bug report (free text + repo) each produce a
  reviewed PR; the bug run commits a test red at the parent commit and green at the fix.
- **RESULT — DONE (2026-11-05).** Shipped `bug_fix` (G2, `workflows/bug_fix.json` + `common/bug_fix.py`
  + the `stages/` workers) — reproduction-first: a bare `coding_agent` authors a failing test, the raw
  `test_run` confirms it **red** at the parent (`bug_repro_gate`; a passing test is an honest
  `cannot_reproduce`), a `code_subtask` fixes the source, `test_cycle` proves **green**, and
  `bug_outcome` emits `FIXED` only if reproduced ∧ green ∧ the repro test was not tampered with (H13).
  Shipped `sdlc_intake` (G1, `workflows/sdlc_intake.json` + `common/intake.py`) — a read-only
  classifier + `intake_route` routing by kind and execution mode (H9/H12), with a bounded clarify gate.
  Proven by real executions: `bug_fix` `e6fcfe0d` **FIXED** (repro test red `assert -1==5` at parent,
  green at the fix, `calc.py` corrected, repro test provably unchanged — independently re-verified);
  `sdlc_intake` routed a bug → `bug_fix` → **FIXED** (`6667bf96`, full G1→G2), a feature → `team_build`
  (`299efdca`), and a forced-single feature → `code_parallel` → **delivered** (`b07a3a8c`, `sub()`
  added, green). (A real trap fixed en route: `code_subtask` self-repairs, so the repro phase must use a
  bare `coding_agent` or it fixes the bug while "writing the test".) Full write-up:
  [`stages_result.md`](stages_result.md). **Phase 5 is unblocked.**

### Phase 5 — Greenfield on-ramp
- **G3 `project_bootstrap`** → hand off to the team cycle.
- *Acceptance:* a one-paragraph description + empty dir → a tested skeleton → the team adds the
  first real feature on top, in one driven sequence.
- **RESULT — DONE (2026-11-05).** Shipped `project_bootstrap` (`workflows/project_bootstrap.json` +
  `common/bootstrap.py` + the `stages/` workers): `bootstrap_init` git-inits the empty dir → a
  `coding_agent` scaffolds a minimal skeleton (code + a passing test + a Makefile `test` target +
  AGENTS.md documenting `make test`) → `commit` → a raw `test_run` gates the skeleton **green** →
  `code_parallel` adds the first feature → `bootstrap_outcome` folds the verdict. Proven by real
  execution `97609b2c`: an empty dir + "a `stringkit` library with `reverse`" + first feature "add
  `shout`" produced **BOOTSTRAPPED** — green skeleton then the feature on top, **4 tests passing**,
  independently re-verified (`reverse` + `shout` present, history shows the feature on the skeleton). Two
  real traps fixed en route: the skeleton's AGENTS.md is candidate-authored (so the verification command
  is supplied explicitly via a raw `test_run` — the G3 intent), and the raw verifier accepts only fixed
  entrypoints (`make`/`pytest`/… not `python3`), so `make test` is used. Full write-up:
  [`bootstrap_result.md`](bootstrap_result.md). **Phase 6 is unblocked.**

### Phase 6 — Program scale & lasting memory
- **G4 `cc_program`** — full **teams-of-teams hierarchy** (H3) over a shared program branch — and
  **G5 `repo_memory`** with **lesson decay/invalidation** (H10) and first-class refactor /
  dependency-upgrade paths (noting refactors are often global and may route to a small/serial team,
  not a wide fan-out).
- *Acceptance:* a multi-subsystem build in dependency order with cross-subsystem integration green
  and a program report; a second run on the same repo plans using prior repo-memory, and a stale
  lesson is demonstrably invalidated rather than trusted.
- **RESULT — DONE (2026-11-05).** Shipped `cc_program` (G4, `workflows/cc_program.json` +
  `common/program.py`): reads `repo_memory` → a `coding_agent` plans subsystems with cross-subsystem
  `dependsOn` + frozen contracts → `team_build` builds them **in dependency order** on a shared program
  branch (the board already does dependency-ordered waves + continuous integration + green gate; H3
  teams-of-teams is the scaling path) → `program_outcome` reports the topological ranks → `repo_memory`
  persisted. Shipped `repo_memory` (G5, `common/repo_memory.py` + `repo_memory_read`/`_write`):
  on-branch invariants + lessons (provenance/confidence/refs) with **decay** (H10 — a lesson whose
  referenced files vanished is invalidated, never trusted). Proven by real execution `c13c626d`:
  **PROGRAM_DELIVERED**, subsystems `model → store → service` built in order, ranks
  `[[model],[store],[service]]`, integration **9 tests green**, 3 lessons persisted — independently
  re-verified. repo_memory proven on the built worktree: a read returns 3 live lessons (a second run
  plans with them), and removing `service.py` invalidates exactly that stale lesson (`live 2,
  invalidated 1`). A real planner defect was guarded en route (`plan_route` drops a contract path that
  is also a subsystem's file — you cannot freeze a file you assign someone to build, H4/H14). Full
  write-up: [`program_memory_result.md`](program_memory_result.md). **All six build phases (0–6) are
  complete.**

---

## 7. Open questions (decide before building each phase)

The *premise* risks — whether the swarm can work at all — are in §4.7 (H1–H10). These are the
remaining *tactical* decisions once the premise holds:

1. **Team-size policy.** What exactly does standup size a wave from — ready-item count, dependency
   width, per-item complexity score, remaining budget? Needs a concrete, testable formula.
2. **Monitor authority.** The precise boundary between what a monitor may change autonomously
   (re-prioritize, nudge) vs. must escalate (kill work, expand scope, spend). Err toward escalation.
3. **Feedback segmentation (H11).** How short must a turn segment be for advisory human/peer
   feedback to feel responsive without re-checking the board so often it dominates cost? And what
   classifies a piece of feedback as *blocking* (worth a live `interrupt()`) vs. *advisory*
   (next segment) — the human's stated urgency, or a judge?
4. **`sdlc_intake`: workflow vs. TUI front door**, and **`bug_fix`: own workflow vs. `code_parallel`
   `mode:bug`** (prefer the mode per the simplicity policy unless the branch count hurts).
5. **`cc_program` prerequisites.** Audit which of `IMPROVEMENTS.md` Phase 3 (artifacts-on-branch,
   failure localization, budget/report) are landed before committing to Phase 6.
6. **Audit & reproducibility.** A nondeterministic swarm of hundreds is hard to reproduce or
   explain. What audit trail (which agent / lesson / directive led to which change) does the board
   must retain to meet this repo's precise-failure-mechanism bar (`AGENTS.md`)?
7. **Web console stack (§4.8).** What serves the Swarm Console — a thin stdlib/live-data page over
   the board store, or a heavier app — and how does it authenticate the steering actions it writes
   back (ties to H16 identity)? Keep it a *view over the boards*, never a second source of truth.

---

## 8. Non-goals restated (so the scope cannot drift)

No deployment, release-to-environment, CD, canary, rollback, runtime monitoring, observability,
incident response, or CI-pipeline authoring. "Monitor agents" watch *construction work in
progress*, not running systems. This document covers construction only: intake → build (as a
team) → verify → review → reviewed PR.
