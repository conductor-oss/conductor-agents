# SDLC Closure Plan — every roadmap hypothesis to ✅

> **Status: DONE (2026-11-06) — all ten work packages built and live-proven; see [`closure_result.md`](closure_result.md).**
> Originally: PLAN (2026-11-06). Companion to [`SDLC_ROADMAP.md`](SDLC_ROADMAP.md). The six build
> phases are shipped and live-proven against their own acceptance criteria. This document is the
> plan + implementation design to close the remaining gap between *those phases* and *the goal* —
> the collaborating swarm described in roadmap §4–§5 — so that every row of the §4.7 hypothesis
> scorecard (H1–H17 + scale) is earned, not asserted. It is written to be executed in order; each
> work package ends with a real Conductor execution as proof, per `AGENTS.md` and
> `feedback_run_full_harness`.

---

## 0. Where we are → where this plan ends

Measured against the full roadmap (a fresh read of the code on 2026-11-06, not the phase write-ups):

| H | Now | Mechanism today | Closed by |
|---|---|---|---|
| H1 write-concurrency | ✅ | continuous rebase + ownership-gated dispatch (`continuous_integrate.py`, `file_ownership.py`) | — (kept; WP5 adds cross-team CAS) |
| H2 convergence | 🟡 | caps exist; **review-cap verdict `changes` → DONE**; **no starvation exit** | WP1 |
| H3 hierarchy | ❌ | one flat board; `cc_program` = one `team_build` (`program.py` docstring) | WP5 |
| H4 frozen contracts | 🟡 | contract edit → BLOCKED, never a controlled event (`team/tasks.py:282`, `_RETRYABLE_REOPEN`) | WP1 |
| H5 hard budget | 🟡 | gate solid; **reviewer/planner/test spend never recorded** (`record_spend` only at `team/tasks.py:338`) | WP1 |
| H6 substrate | ❌ | local dir + S3; every fold re-reads + re-parses the whole log; no Postgres | WP4 |
| H7 rollup/exceptions | ✅ | rollup + exception queue + console + push | — |
| H8 review not theater | ✅ | bounded, different agent, hidden `reviewCriteria` | WP1 removes the one hole |
| H9 zero ceremony | ✅ | `team_route` → `code_parallel`, zero facts | — |
| H10 lesson decay | 🟡 | file-existence only | WP8 |
| H11 feedback latency | ❌ | one opaque `code_subtask` per item; feedback reaches an item at the *next standup* | WP2 |
| H12 three modes | ❌ | two modes: `team` iff `item_count > 1` (`intake.py:49`); no parallel-independent route; no best-of-N | WP3 |
| H13 gameable check | 🟡 | reviewer≠author, hidden criteria, bug-fix tamper guard; implementer still authors the item's tests | WP6 |
| H14 decomposition gate | ❌ | none; overlap discovered at dispatch (ownership serialization) | WP3 |
| H15 shared error | 🟡 | provenance yes; lessons are templated status lines; no retraction | WP8 |
| H16 constraint lifecycle | ❌ | `authority` is a self-declared string; no identity; sticky forever; no pushback | WP7 |
| H17 durability/sandbox | 🟡 | agent sessions **are** OS-sandboxed (`coding_agent.py` `sandbox_enabled`); checks are not; no checkpoints | WP2 + WP9 |
| Scale | 🟡 | 8-wide proven | WP4 + WP5 (30-wide) |

**End state:** every row ✅ with a cited execution id in a `closure_result.md`, the roadmap header
updated, the OpenSpec change archived, and the suite green.

---

## 1. Principles that bound every design below

From `AGENTS.md` and roadmap §4.6 — restated because each work package is checked against them:

1. **One version (v1), zero `JSON_JQ_TRANSFORM`, no timeouts/deadlines.** Every decision is a
   worker-computed value or a `SWITCH`; loops are bounded by counts, never by clocks.
2. **Simplicity-justified additions.** §4 keeps a ledger: every new task, branch, input, fact kind,
   or exception kind is named with the requirement that could not be met without it.
3. **One shared-state abstraction.** Everything new is a *kind* on the one append-only log and a
   *fold* in `team_board.py` — no second store, no mutable shared object, no locks.
4. **Boards are the single source of truth; every UI action is an authored board write.** Nothing
   below gives the console or TUI state of its own.
5. **Honest outcomes, precise mechanisms.** Every new terminal state names its mechanism
   (`review_rounds_exhausted`, `dependency_starved`, `contract_change_pending`, …). No silent accepts.
6. **Child processes via `common/exec.py` / `check_execution.py`.** No new raw `subprocess` path.
7. **Prove through the harness.** Each WP ends with real Conductor executions; no bespoke scripts.
8. **Gap-closure.** A limit that is genuinely external is named with the exact removal step and the
   largest reachable increment proven now (§5).

---

## 2. Work packages (in execution order)

Each WP has: *Mechanism* → *Data (facts/shapes)* → *Code* → *Workflow* → *Tests* → *Live proof* →
*Exit criterion*. File paths are relative to `coding-harness/`.

### WP1 — Convergence & budget defects (H2, H4, H5, H8) — *start here*

Four verified defects, each contained, each with a live re-proof.

#### WP1.1 Review cap → quarantine, never a silent accept (H2/H8)

**Mechanism.** `maxReviewRounds` is redefined as *the number of `changes` verdicts an item may
absorb*. Every item in `review` is **always** reviewed (today `team_review_build` skips items at the
cap and `team_apply_reviews` stamps them DONE — `team/tasks.py:383`, `:481-485`, `:492-500`). The
(N+1)th `changes` verdict **quarantines** the item: `blocked` with reason `review_rounds_exhausted`
plus an exception `review_cap` for a human. A reviewer task that fails or returns no parseable verdict
is `unavailable`, not `lgtm` (`_review_fields` today defaults to lgtm — `team/tasks.py:68`): the item
stays in `review` and is re-reviewed next wave; after `maxReviewAttempts` (2) unavailable verdicts it is
`blocked` with reason `reviewer_unavailable` + exception.

**Data.** `review` fact `verdict ∈ {lgtm, changes, unavailable}`. Exception kinds `review_cap`,
`reviewer_unavailable`. Console steer action `accept` (a human marks a quarantined item `done`:
`set_state(... DONE, by=<human identity>, reason="accepted at review cap by <id>")`).

**Code.** `team/tasks.py`: `_review_fields` → `unavailable` default; `team_review_build` reviews every
`review` item; `team_apply_reviews` branches on (verdict, prior `changes` count); delete the
"accept (review cap)" sweep. `common/monitor.py`: quality nudge only for items in `OPEN_STATES`
(today it nudges DONE items — dead signal). `tui/board.py` + `tui/console.py`: `accept` action.

**Tests.** `test_team_cycle.py`: (N+1)th changes → blocked + `review_cap`; lgtm after N changes →
done; unavailable → stays review, then `reviewer_unavailable`; no DONE from a `changes` verdict under
any input. `test_monitor.py`: no nudge on done/blocked.

**Live proof.** The hidden-`reviewCriteria` task (shape of run `cf79c731`) with an unsatisfiable
criterion and `maxReviewRounds=1` → `INCOMPLETE` naming `review_rounds_exhausted`, the exception in the
console; a human `accept` from the console → next wave `done` → `DELIVERED`. A second run where the
reviewer task is made to fail (bad model id on the review fork) → `reviewer_unavailable`, never
`DELIVERED`.

#### WP1.2 Spend completeness (H5)

**Mechanism.** Every agent task's cost is recorded at the join that consumes it. Today only
implementer forks are (`team/tasks.py:336-343`). Add: reviewers (`team_apply_reviews` over the review
join), the planner (`team_plan_init` gets `planUsage` from `cc_program`/`sdlc_intake`), the
acceptance-test authors (WP6), child teams (WP5), and the final `test_cycle` (its repair agents) via a
new `costUsd`/`tokenUsed` on `test_cycle_outcome` folded from the existing `usage_accumulate` records.

**Code.** `team/tasks.py` (`apply_reviews`, `plan_init`, `outcome`), `verification/tasks.py`
(`test_cycle_outcome` usage fold), `workflows/test_cycle.json` outputs. `budget_gate.read_spend`
report groups authors by role prefix (`implementer:`, `review_`, `planner`, `tests`, `child:`).

**Tests.** apply_reviews records spend; a review-only wave moves the gate `ok → halt`; the report
lists per-role totals that sum to the total.

**Live proof.** Ceiling set so implementer spend < ceiling but implementer + reviewer ≥ ceiling →
halt after wave 1's review (`dispatchNewWork=false`), report shows reviewer spend; `INCOMPLETE` names
the budget halt.

#### WP1.3 Contract change as a controlled event (H4)

**Mechanism.** A fork that edits a frozen contract path is not merged and not retried; it becomes a
**decision** on the board. `team_integrate` keeps the fork's branch/worktree, sets the proposer
`blocked` with reason `contract_change_pending`, and posts exception `contract_change` {item, paths,
branch, head, diffstat}. A human resolves it from the console as `approve` or `reject`. The **next
`team_standup` applies** resolved decisions (the console only writes; the Lead executes — principle 4):

- **approve** → the fork is integrated by continuous rebase (conflict → proposer `reopened`,
  retryable); a new `contract` fact `{paths, frozen: true, revision: n+1, approvedBy, head}` is
  appended; **dependents are re-planned**: every item in `done`/`review` is moved to `reopened` with a
  `lead` directive "contract <paths> changed in <head>; re-verify against the revised contract"; items
  not yet built receive the directive only. The proposer goes to `review` like any delivery.
- **reject** → proposer `reopened` (attempt+1) with a human directive "the contract is frozen; do not
  edit <paths>"; its fork branch/worktree is removed so the next dispatch starts clean.
- Re-plans caused by contract events do not consume an item's failure attempts; they are counted
  separately (`contractReplans` on the item fact) and capped by `maxContractEvents` (2) → beyond it,
  exception `contract_churn` (the H2 anti-thrash damper for interface churn).

With no human (`humanAvailable=false`) a pending contract decision is a starvation exit (WP1.4) naming
`contract_change_pending` — honest, not a hang.

**Code.** New pure `common/contract_event.py`: `pending_decisions(board) -> [...]`,
`plan_decision(decision, board, contract) -> {ops, newContract}` (board ops the worker applies);
`team/tasks.py` `integrate` (keep branch, post exception) and `standup` (apply decisions first).
`tui/console.py`: `approve_contract`/`reject_contract` → `resolve_exception(..., resolution=...)`.

**Tests.** integrate posts the exception and keeps the branch; standup applies approve (dependents
reopened with the directive, contract revision 2) and reject (clean re-dispatch, constraint attached);
`maxContractEvents` → `contract_churn`.

**Live proof.** 3-item task where item B's acceptance criterion requires a new parameter on the
frozen interface. Run 1 (approve from the console): A `done → reopened` with the directive, B
integrated → reviewed, contract revision 2 on the board, `DELIVERED`. Run 2 (reject): B re-dispatched
with the constraint and delivers without touching the contract (or ends honestly `blocked`).

#### WP1.4 Starvation exit + blocking human gate (H2)

**Mechanism.** The wave loop (`team_build.json:259`) exits today only on `maxWaves`, `drained`, or a
budget halt. A board whose open items all depend on a `blocked` item spins empty waves to `maxWaves`
and then misnames the mechanism. `team_standup` now reports `starved` + `starvedBy ∈ {dependency,
ownership, exceptions}` whenever it dispatches nothing on an undrained board; `capture_round` carries
it into the loop condition (`&& $.starved === false`); `team_outcome` names it
(`dependency-starved: items X depend on blocked Y`, `awaiting a human decision on N exception(s)`).

When a human is available (`humanAvailable=true`) and starvation is caused by open exceptions, the run
must **wait, not exit** (roadmap §4.4: the few decisions that must block). One `SWITCH` + one `WAIT`
(`human_decision`) at the end of the loop body, released from the console/TUI (`/api/release` →
Conductor task completion, the same call the TUI's existing WAIT gates use). On release the next
standup applies the decision and the loop continues. `starved` is set only when the gate did not wait.

**Code.** `team_board.starvation(items, open_exceptions) -> (starved, by, detail)` (pure);
`team/tasks.py` standup/outcome; `workflows/team_build.json` (variable `starved`, loop condition,
`human_gate` SWITCH → `human_decision` WAIT); `tui/console.py` + `tui/api.py` release. `workflow-inputs.md`.

**Tests.** pure starvation classification; structural: loop condition includes `starved`, WAIT present
and documented; outcome names the mechanism.

**Live proof.** B dependsOn A, A unsatisfiable → `blocked`; with `maxWaves=12` the run ends after
**2** waves (not 12) naming `dependency_starved`. Same board with `humanAvailable=true` → the run holds
at `human_decision`; the human reopens A with a directive from the console and releases → the run
continues → `DELIVERED`.

**Exit criterion for WP1:** all four proofs cited; suite green; roadmap §4.7 H2/H4/H5/H8 rows ✅.

---

### WP4 — Substrate at scale (H6) — *second, because every later WP folds more often*

**Mechanism.** The log is append-only, so a fact never changes once written: a **process-local
incremental fold cache** parses each fact exactly once. `read_facts(cid, kind)` lists keys, parses only
unseen keys, and serves the rest from cache; a per-process LRU over runs bounds memory. Listing a
kind is a single-directory `scandir` (today `os.walk` from the prefix — `blackboard.py:107`). A
**Postgres backend** (`SWARM_BLACKBOARD_PG_DSN`, psycopg 3, lazy import like boto3) implements the same
`put/get/list` plus `list_since(prefix, ts)`; it can live in the Conductor server's Postgres (roadmap
H6's "the harness's Postgres backend") or any DSN. The ownership fold in `team_standup` reads the
ownership log **once** per standup and simulates claims on the in-memory owner map
(`file_ownership.plan_claims`) instead of re-folding per file (`team/tasks.py:180-184`).

**Data.** Postgres table `swarm_blackboard(key text primary key, cid text, kind text, ts double
precision, body bytea)`, index `(cid, kind, ts)` and a `text_pattern_ops` index on `key` for prefix
listing. Keys/records byte-identical to the file backend.

**Code.** `workers/common/blackboard.py` (`_FactCache`, `read_facts(..., since=None)`,
`_LocalBackend.list` scandir fast-path, `_PostgresBackend`, `clear_cache()`), `common/file_ownership.py`
(`events`, `plan_claims`), `team/tasks.py` standup. `requirements.txt`: `psycopg[binary]` as an optional
extra. Security-harness behavior unchanged (its tests remain the regression gate, as in Phase 0).

**Tests.** cache parses each fact once (count assertion, not timing); `since` semantics; Postgres
backend contract tests run when `SWARM_BLACKBOARD_PG_DSN` is set (skipped — and reported skipped —
otherwise); owner-map simulation equals the fold after the claims are posted; a synthetic 1,000-item /
10,000-fact board folds through `fold_board` with one parse per fact.

**Live proof.** The Phase-2 8-wide run re-executed on the Postgres backend → `DELIVERED 8/8`; the
30-wide run in WP5 runs on it. Standup task durations before/after are reported from the Conductor
execution (measured, not asserted).

**Exit criterion:** H6 ✅ (Postgres + incremental fold + single ownership fold); the exact fleet
limit for width is named in §5 with its removal step.

---

### WP2 — Live feedback inside the session + commit-or-lose (H11, H17a, §7 open question #3)

**Mechanism.** The implementer's `coding_agent` task *already* holds one long-lived SDK session for
the whole item (up to `maxTurns`), so the roadmap's cost argument against streaming sessions is
already paid. Switch `_run_claude` from single-message `sdk.query(prompt=str)` (`coding_agent.py:270`)
to a **streaming `ClaudeSDKClient` session**. At every turn boundary the worker **polls the board** for
new directives addressed to its item/team since its last poll (`common/feedback.py`). Advisory
feedback is **queued into the running session** as a user message ("NEW FEEDBACK (human): …") and
lands on the next turn — latency = one turn, inside the same item. Blocking feedback (`urgency:
blocking`, the console's `stop` action) **`interrupt()`s** the session; the worker commits the
checkpoint and returns `interrupted`, and the item is `reopened` with the directive attached. This
closes both H11 paths with zero new Conductor tasks; open question #3 is answered: *advisory = next
turn, blocking = interrupt; the author states urgency, no judge*.

**Commit-or-lose (H17a).** At every `checkpointEveryTurns` boundary (team default 10) and at session
end the worker commits a dirty worktree (`wip(<item>): checkpoint turn N`, via `common/git.py` →
`exec.py`). The integrator **squashes** each item's fork to one commit on the shared branch
(`continuous_integrate.integrate_ready(..., squash=True)`), so history stays one-commit-per-item and
linear. A worker lost mid-item costs at most `checkpointEveryTurns` turns: on re-execution the worker
finds the wip commits on the same worktree and prefixes the prompt with "progress already committed:
<log>"; with a shared `session_store` it also resumes the session (`resumeSessionId`, existing).

**Data.** `directive` gains `urgency: advisory|blocking` (default advisory). `coding_agent` inputs
`feedbackCorrelationId`, `feedbackItem`, `feedbackEveryTurns` (1; 0 = off), `checkpointEveryTurns`
(0 = off); `code_subtask` passes them through; `team_standup` sets them. Output `turn_log` records each
injection `{turn, directiveKeys}` and `interruptedBy`.

**Code.** `common/coding_agent.py` (`ClaudeSDKClient`, poll/queue/interrupt at turn boundaries,
checkpoint commits), `common/feedback.py` (pure `poll(directives, item, since_ts) → {advisory,
blocking, latest_ts}`), `common/continuous_integrate.py` (squash), `workflows/taskdefs/coding_agent.json`
+ `workflows/code_subtask.json` (pass-through inputs), `team/tasks.py` standup, `tui/console.py` (`stop`
action = blocking directive). Codex/Gemini backends: feedback is injected at their next resume
boundary (their SDKs expose no queue/interrupt — named limit, §5).

**Tests.** `feedback.poll` (since-ts, target matching item/`team`/`agent:<id>`, urgency split);
`coding_agent` with a fake client: advisory queued at turn k, blocking interrupts within one turn;
checkpoint commits every K turns in a tmp repo; `integrate_ready(squash=True)` yields one commit per
item and a linear history; `turn_log` carries the injected directive keys.

**Live proof.** `team_build` run; while item A's agent is mid-session the human posts an advisory
nudge from the console → A's `turn_log` shows the injection and A's delivered diff reflects it in the
**same wave**; a `stop` on item B → B's session is interrupted within one turn, B is `reopened` with
the directive, its checkpoint commit is on the fork branch. Latency (directive ts → injection ts) is
reported in `closure_result.md`. A worker process killed mid-item: after supervisor restart and
workflow retry the item resumes from its last checkpoint (wip commit present, no lost work beyond the
checkpoint interval).

**Exit criterion:** H11 ✅, H17 durability half ✅, §7 #3 closed in the roadmap.

---

### WP3 — Three execution modes, best-of-N, decomposition gate (H12, H14, Gate 0)

**Mechanism.** The mode is read off the decomposition (roadmap §4.7 H12c/H14). A pure
`common/decomposition.py` assesses a plan: dependency edges, files shared between items, shared files
that are neither contract paths nor ordered by `dependsOn` (**uncontracted overlap** — today
discovered only by ownership serialization at dispatch), cycles, items with no files, item count.
Verdict `ok|replan` with precise reasons; `mode(assessment)`:

- `single` — one item (H9, unchanged),
- `parallel` — ≥2 items, **no** edges and **no** shared files → `code_parallel` with
  `usePrecomputedPlan` (its existing input) — the parallel-independent engine, zero boards,
- `team` — any edge or any shared file → `team_build`.

`sdlc_intake` and `cc_program` run the assessment after the planner; `replan` loops back to the
planner once with the reasons injected (bounded `maxReplan`=2), then terminates honestly as
`plan_rejected` naming the reasons. `team_plan_init` refuses a `replan` plan the same way, so no agent
is ever spawned on a bad cut. `team_build`'s own `mode_gate` gains the `parallel` case, so a caller
that hands it a disjoint plan gets the cheap engine.

**Best-of-N inside the swarm (H12a).** `team_build` input `bestOf` (default 1 — today's behavior).
`team_standup` dispatches `bestOf` candidate forks per item (`<item>_c<k>`, separate worktrees and
branches; **one** ownership claim per item). `team_integrate` picks the winner with a pure rule
(`pick_best`: delivered ∧ subtask tests green ∧ delivery audit clean, then fewest changed files, then
smallest diff, then lowest cost), integrates only the winner, discards the losers' worktrees, records
every candidate's spend. No candidate delivered → the existing failure path with the best candidate's
mechanism.

**Gate 0.** The router emits `routeEvidence` (edges, shared files, contract paths) and every outcome
carries `mode`. Gate 0 is answered empirically in both directions by the live proof below (the coupled
direction was already shown by Phase 1: `code_parallel` ended `implementation_unavailable` on the
overlap task).

**Code.** `common/decomposition.py` (new, pure), `common/intake.py` (`decide_route`/`plan_route` use
it; routes `clarify|bug|greenfield|single|parallel|team|replan`), `common/program.py`
(`topo_ranks` also splits rank-mates that share files — used by WP5), `workflows/sdlc_intake.json`
(bounded replan `DO_WHILE` around classify→route; `parallel` case → `code_parallel`), `workflows/cc_program.json`
(same gate), `workflows/team_build.json` (`parallel` case, `bestOf`), `team/tasks.py` (candidates,
`pick_best`), `common/team_board.py` (`dispatched` entries carry candidates).

**Tests.** one test per decomposition rule; mode mapping; intake routes `parallel`/`replan`/`team`;
standup candidate refs and single claim; `pick_best` ordering; structural: SWITCH cases, replan loop
bounded, input docs.

**Live proof (Gate 0).** (a) a file-disjoint 3-item feature through `sdlc_intake` → `parallel` →
delivered by `code_parallel` with **zero** blackboard facts; (b) the forced-overlap registry task
through `sdlc_intake` → `routeEvidence.sharedFiles` → `team` → `DELIVERED`; (c) a planner output with an
uncontracted overlap → `replan` once → fixed plan → routed; (d) `team_build` with `bestOf=2` on a 2-item
task: two candidates per item on the board, winner chosen by the rule, losers discarded, `DELIVERED`.

**Exit criterion:** H12 ✅, H14 ✅, Gate 0 recorded with execution ids.

---

### WP6 — Separate the test author from the check (H13)

**Mechanism.** Reuse the Phase-1 contract shape ("an interface file plus the shared tests that pin
it") and the `bug_fix` repro-first pattern: **acceptance tests are authored before implementation by a
different agent and frozen**. After `team_plan_init`, a dynamic fork of bare `coding_agent` authors
(tools `Read/Grep/Glob/Write/Edit`, write roots = test paths) writes tests from each item's
`acceptanceCriteria` without implementing. A raw `test_run` confirms each file is **red** at the base
(a green-at-base acceptance test is tautological → discarded + lesson `acceptance_test_tautology`), the
red tests are committed to the shared branch, and their paths are **appended to `contractPaths`**. From
then on the integrator's existing contract check makes an implementer's edit of its own acceptance test
a controlled event (WP1.3) — the separation is enforced, not advisory. Per-item `code_subtask` runs
`test_cycle` with `testCommands` targeting that item's acceptance tests (other items' red tests must not
fail it); the final team `test_cycle` runs the whole suite. The reviewer prompt additionally audits any
test in the diff (a test that cannot fail is a blocking finding).

**Data.** `team_build` inputs `authorAcceptanceTests` (default true when any item has
`acceptanceCriteria`), `testRoots` (default `tests/`); `item` fact gains `acceptanceTests: [paths]`.

**Code.** `team/tasks.py`: `team_acceptance_build` (fork builder) and `team_acceptance_commit` (red
gate + commit + contract extension) — two new SIMPLE tasks justified by name: no existing task authors
tests before implementation against a plan, and the red gate must run on the committed shared branch
before any implementer is dispatched; `team_standup` passes `testCommands`; `code_subtask.json` passes
`testCommands` through to its `test_cycle` (new pass-through input). `workflows/team_build.json`.

**Tests.** fork build per item with criteria; red gate discards a green test; contract paths
extended; standup emits per-item `testCommands`; reviewer prompt contains the test-audit clause.

**Live proof.** 2-item task: acceptance tests authored red at plan time (commit on the branch,
`contractPaths` extended), each item turns its own tests green on tests it did not write, final suite
green → `DELIVERED`. An implementer prompted to "fix the test instead" lands in `contract_change_pending`
(controlled event), never `done`.

**Exit criterion:** H13 ✅ structurally (author ≠ check enforced by the contract mechanism).

---

### WP5 — Teams-of-teams and the scale proof (H3, scale)

**Mechanism.** Hierarchy = **nested `team_build` runs with scoped boards**, linked by facts.
`cc_program` keeps a **program board** (cid = program workflow id) holding one item per subsystem and
runs ranks in order; within a rank, independent subsystems are built **concurrently by child
`team_build` sub-workflows**, each with its own board (cid = its own workflow id) and its own Lead,
monitor, reviewers. The program Lead folds only subsystem items + child rollups (O(#subsystems)); each
team Lead folds only its own items. Depth is recursive by construction (a subsystem whose plan carries
`subsystems` is built by a child `cc_program`), bounded by `maxDepth` (default 2).

Two things make concurrent child teams on one program branch safe:
- **Cross-team ownership is program-scoped**: a child claims files on `parentCorrelationId` when set
  (`ownershipCid = parentCorrelationId or cid`), so two teams never hold the same file.
- **Concurrent integrators use compare-and-swap on the shared branch**: `continuous_integrate` advances
  the branch with `git update-ref <branch> <new> <expected-old>`; if another integrator moved it, the
  rebase is redone onto the new head (bounded by attempts, no sleep/deadline).

Rank construction (`program.topo_ranks`) additionally separates rank-mates that share files unless the
shared file is a program contract (WP3's assessment), so rank-mates are file-disjoint or contract-
mediated by construction.

**Steering across levels.** A program-board directive targeted `subsystem:<id>` or `team` is folded by
the child's standup (one extra read on `parentCorrelationId`) and injected like a local one. Child
rollups and exceptions are mirrored to the program board at rank join (`program_apply_rank`); the
console renders the **team tree** (program run → child runs) from `child` facts and links into each
child's existing board view; push notifications iterate children.

**Data.** Program-board facts: `item` per subsystem (`files`, `dependsOn`, `rank`), `child`
`{subsystem, childRun, depth}`, `rollup` `{subsystem, childRun, rollup, spendUsd}`; child spend recorded
on the program budget under `child:<subsystem>`. `team_build` inputs `parentCorrelationId`,
`subsystemId`; `cc_program` inputs `maxDepth`, `maxWidth` per rank.

**Code.** `common/program.py` (rank split, pure `rank_fork(subsystems, rank, depth)`),
`stages/tasks.py`: `program_rank_init`, `program_rank_build`, `program_apply_rank` (three new SIMPLE
tasks, justified by name: the program board must be seeded, a per-rank dynamic fork built, and child
outcomes folded — none of which an existing task does), `workflows/cc_program.json` (rank `DO_WHILE`
bounded by rank count + `FORK_JOIN_DYNAMIC`), `common/file_ownership.py` (ownership cid),
`common/continuous_integrate.py` (CAS + re-rebase), `team/tasks.py` (parent directives), `tui/console.py`
+ `tui/board.py` (team tree).

**Tests.** rank split by shared files; rank fork shape; apply-rank fold (done/blocked, rollup, spend);
parent-directive fold; CAS: a simulated concurrent branch move forces exactly one re-rebase and ends
linear; depth bound.

**Live proof.** (1) Program with 2 ranks: rank 1 = two subsystems built **concurrently** by two child
teams on one program branch (observed CAS retry in the integrator logs or a deliberately induced race),
rank 2 depends on both → `PROGRAM_DELIVERED`, integration green, program board = 3 items + 3 child
rollups; a directive posted on the program board reaches a child standup. (2) A 3-level run (program →
program → team) with trivial items proves recursion. (3) **Scale 30**: a 30-item plan at `maxWidth=30`
on the Postgres backend (WP4) → `DELIVERED 30/30`; the exact fleet limit hit on the way is named in §5
with the step that removed it.

**Exit criterion:** H3 ✅, scale ✅ at 30 with the next increment's blocker named.

---

### WP7 — Identity, constraint lifecycle, agent pushback (H16, §7 #7)

**Mechanism.**
- **Identity.** An actor registry (`$SWARM_ACTORS_FILE`, default `~/.conductor-code/actors.json`:
  `{"tokens": {"<token>": {"name", "email", "authority": "human"}}}`) is the *configured identity* the
  team trusts (the security-swarm precedent). The console's `POST /api/steer` requires
  `Authorization: Bearer <token>`; unknown/missing → `401`; the written `author` is `human:<email>`,
  never a caller-supplied string (today `apply_steer` self-declares `"human"` — `console.py:81`). The
  TUI uses `SWARM_ACTOR_TOKEN`. Workers derive **authority from the author**, and a `human:` author not
  present in the registry ranks as `agent`.
- **Lifecycle.** Directives carry `expiresAfterWave` (optional) and `supersedes` (a directive key);
  a `lift` op retires one. `team_board.active_directives(directives, wave)` is the only fold anyone
  reads (standup, feedback poll, console). The console lists active constraints per item with
  lift/replace.
- **Over-constraint detection.** The monitor raises exception `over_constrained` when an item carries
  ≥ `maxHumanConstraints` (3) active human constraints, or has been reopened ≥ `stallAttempts` times
  while carrying human constraints (the constraint is the thrash source). Lifting is above the
  monitor's authority — it escalates.
- **Pushback.** The implementer prompt instructs: a listed constraint judged harmful or impossible is
  neither silently obeyed nor overridden — stop and return `pushback {directive, reason}`.
  `code_subtask` gains an optional `outputSchema` pass-through and surfaces `structured`;
  `team_integrate` turns a pushback into `blocked` (`constraint_pushback`) + exception `pushback` with
  the reason; a human lifts/changes the constraint and reopens.

**Code.** `common/actors.py` (new, pure resolve + file read), `tui/console.py` (auth, lift/replace,
`stop`), `tui/board.py`, `common/team_board.py` (`active_directives`, authority derivation),
`common/monitor.py` (`over_constrained`), `team/tasks.py` (prompt clause, pushback path),
`workflows/code_subtask.json` (`outputSchema`, `structured` output).

**Tests.** registry resolve; console 401/200 and author stamping; authority derivation (unregistered
`human:` → agent); active-directive fold (lift/supersede/expire); `over_constrained`; pushback → blocked
+ exception.

**Live proof.** Unauthenticated steer → `401`; authenticated nudge lands as `human:viren@orkes.io`
(the user's own identity; used only for authorship); a sticky constraint lifted mid-run is absent from
the next dispatch prompt (`dispatch` fact, WP10); an impossible human constraint → the implementer pushes
back → exception → the human lifts it → `DELIVERED`.

**Exit criterion:** H16 ✅, §7 #7 closed (stdlib console + bearer identity, view over the boards).

---

### WP8 — Real lessons, retraction, symbol-level decay (H10, H15)

**Mechanism.** Implementers and reviewers return `lessons: [{text, refs}]` in their structured output
(`outputSchema` from WP7); the integrator/review-applier posts them with provenance
`implementer:<id>`/`reviewer:<id>` and confidence capped at `medium` (unverified opinion — H15); only
outcome-derived lessons (integrated, green, reviewed) are `high`/`verified`. The standup digest labels
`[verified]`/`[opinion]`, and an opinion is never promoted to a directive. **Retraction:** a reviewer
may return `retract: [lessonText]`; `apply_reviews` appends a `lesson` fact `{op: retract, text}` and
`team_board.live_lessons` drops it swarm-wide from the next standup. **In-run decay:** lessons whose
`refs` no longer resolve on the shared branch head are dropped from the digest (same helper as
repo_memory). **Symbol-level refs:** `path::symbol`; `common/lesson_refs.verify(repo, ref)` checks the
file exists and, for a symbol, that `\bsymbol\b` occurs in it (language-agnostic) — used by
`repo_memory.decay_lessons` (today file-existence only, `repo_memory.py:89`) and in-run.

**Code.** `common/lesson_refs.py` (new, pure + file read), `common/team_board.py` (`live_lessons`,
retract op, labels), `common/repo_memory.py` (symbol decay), `team/tasks.py` (post agent lessons,
retraction).

**Tests.** opinion cap; retract fold; in-run decay; symbol decay (renamed symbol in an existing file is
invalidated — the case file-level decay misses); digest labels.

**Live proof.** An implementer's opinion lesson appears `[opinion]` in wave 2's prompt; a reviewer
retracts a wrong lesson and it is absent from wave 3's prompt; `repo_memory` invalidates
`service.py::handle` after `handle` is renamed while `service.py` still exists.

**Exit criterion:** H10 ✅, H15 ✅.

---

### WP9 — Sandboxed checks (H17b)

**Mechanism.** Agent sessions are already OS-sandboxed (Seatbelt on macOS, bubblewrap on Linux,
`allowUnsandboxedCommands: false` — `coding_agent.py:539-551`). Checks are not: `test_run` executes
repo test commands through `check_execution.execute` unconfined. Add
`check_execution.sandboxed_argv(argv, cwd, state_dir, *, allow_network=False)`: macOS wraps with
`sandbox-exec -p <profile>` (read everywhere; write only `cwd`, `TMPDIR`, and the runtime caches
`isolated_environment` already places under `STATE_DIR`; no network); Linux wraps with `bwrap`
(`--ro-bind / /`, `--bind cwd`, `--bind state`, `--tmpfs /tmp`, `--unshare-net`, `--die-with-parent`).
`execute` composes it when `sandboxChecks` is on (team runs default on); no new process-launch path.

**Code.** `common/check_execution.py`, `verification/tasks.py` (`test_run` flag), `workflows/test_cycle.json`
+ `team_build.json` (`sandboxChecks` pass-through).

**Tests.** a check that writes outside `cwd` fails under the sandbox; a check that opens a socket fails;
a normal `pytest` passes; argv composition per platform (pure).

**Live proof.** `runtime_health` executed with `executionHealthy` and `verificationHealthy` true
(policy for any command-launch change); a team run with `sandboxChecks=true` ends `DELIVERED`.
External limit: Linux hosts need `bubblewrap` installed (§5).

**Exit criterion:** H17 ✅ (durability from WP2 + sandboxed checks here).

---

### WP10 — Audit trace, housekeeping, closure (§7 #6, code quality)

- **Provenance/audit.** `team_standup` appends a `dispatch` fact per dispatched candidate: `{item,
  attempt, candidate, waveBase, contractRevision, promptSha256, model, lessonKeys, directiveKeys}` —
  the link *which lesson/directive led to which change*. `tui/trace.py` folds an item's timeline
  (states, dispatches with injected keys, feedback injections from `turn_log`, reviews, lessons,
  checkpoints, spend) → console `/api/trace?run=&item=` and TUI drill-down. Reproducibility: a
  dispatch names base commit + prompt hash + model; the residual nondeterminism is the model's (named).
- **Helpers.** `common/shapes.py` (`mapping/listed/text/num/truth/iter_joined`) replaces the four
  duplicated copies and the seven underscore-private imports from `campaign.tasks` in `team/tasks.py`.
- **Message bus.** `send_message`/`read_inbox` stay in the substrate for the security harness; the
  construction team's one channel is directives (recorded as a deliberate simplicity decision in
  `team_board.py`'s docstring).
- **Closure.** `closure_result.md` with every execution id; roadmap header + §4.7 scorecard + §7
  (#3, #6, #7 closed) updated; OpenSpec change `swarm-substrate-and-convergence-spike` archived; fleet
  restarted after taskdef changes (`register.sh`); the stray `mkdocs serve` stopped; invariant audit
  clean.

---

## 3. Sequencing, dependencies, proof matrix

```
WP1 (defects) ─▶ WP4 (substrate) ─▶ WP2 (feedback+checkpoints) ─▶ WP3 (modes+gate)
      ─▶ WP6 (check) ─▶ WP5 (hierarchy+scale 30) ─▶ WP7 (identity) ─▶ WP8 (lessons)
      ─▶ WP9 (sandboxed checks) ─▶ WP10 (trace, closure)
```

Why this order: WP1 fixes verdict correctness (everything after it is proven against honest verdicts);
WP4 makes folds cheap before WP2 polls per turn and WP5 runs 30-wide; WP2 before WP3 so best-of-N
candidates already checkpoint and take feedback; WP6 before WP5 so child teams inherit the check;
WP7/WP8 need WP7's `outputSchema` pass-through; WP9 is independent and last among the mechanisms.

Every WP ends with: `make test` green → `register.sh` → the named live runs → a section in
`closure_result.md` → the roadmap row flipped. No WP starts before the previous one's proofs are cited.

| WP | Hypotheses | New tasks | New inputs | Live runs |
|---|---|---|---|---|
| WP1 | H2 H4 H5 H8 | `human_decision` WAIT + `human_gate` SWITCH | `humanAvailable`, `maxReviewAttempts`, `maxContractEvents`, `planUsage` | 6 |
| WP4 | H6 | — | env `SWARM_BLACKBOARD_PG_DSN` | 1 |
| WP2 | H11 H17a | — | `feedbackEveryTurns`, `checkpointEveryTurns` (+ cid/item pass-through) | 2 |
| WP3 | H12 H14 | replan `DO_WHILE` (intake, program) | `bestOf`, `maxReplan` | 4 |
| WP6 | H13 | `team_acceptance_build`, `team_acceptance_commit` | `authorAcceptanceTests`, `testRoots`, `testCommands` pass-through | 2 |
| WP5 | H3 scale | `program_rank_init`, `program_rank_build`, `program_apply_rank` | `parentCorrelationId`, `subsystemId`, `maxDepth` | 3 |
| WP7 | H16 | — | `outputSchema` pass-through, `maxHumanConstraints` | 2 |
| WP8 | H10 H15 | — | — | 2 |
| WP9 | H17b | — | `sandboxChecks` | 2 |
| WP10 | §7 #6 | — | — | 0 |

---

## 4. Simplicity ledger (every addition, justified by name)

- **`human_decision` WAIT + `human_gate` SWITCH (WP1.4):** the roadmap requires a few decisions that
  *block* (a contract change, a quarantined item); exiting and restarting would lose the run's
  correlation id and therefore its board. No existing task in `team_build` can hold the run.
- **replan `DO_WHILE` in `sdlc_intake`/`cc_program` (WP3):** a bad decomposition must be re-planned
  *before* any agent is spawned (H14); the planner is an agent task, so re-planning is a bounded loop
  around it, not a flag.
- **`team_acceptance_build` / `team_acceptance_commit` (WP6):** no existing task authors tests from a
  plan before implementation and gates them red on the shared branch; two tasks because the fork
  builder and the post-join red gate/commit have different retry value.
- **`program_rank_init` / `program_rank_build` / `program_apply_rank` (WP5):** seeding a program board,
  building a per-rank dynamic fork of child teams, and folding child outcomes have no existing
  equivalent (`cc_program` today makes a single `team_build` call).
- **Fact kinds:** `dispatch` (provenance; §7 #6), `child`/`rollup` on the program board (H3).
  **Exception kinds:** `review_cap`, `reviewer_unavailable`, `contract_change`, `contract_churn`,
  `pushback`, `over_constrained`. **Directive fields:** `urgency`, `expiresAfterWave`, `supersedes`,
  op `lift`. **Lesson op:** `retract`. All are kinds/fields on the one log — no second store.
- **Not added:** a Lead/Architect agent per wave (the pure fold + planner stays), a second channel
  (the message bus stays unused by construction), compaction/snapshots (the incremental cache makes
  them unnecessary), timeouts anywhere.

---

## 5. External limits — named, with the removal step

- **Wave width is bounded by the `coding_agent` worker's thread count**: `thread_count=8`
  (`workers/coding_agent/tasks.py:506`), overridable per host with
  `CONDUCTOR_WORKER_THREADS_CODING_AGENT=<n>` (`common/polling.py:44-53`), and by host RAM (~1 GiB per
  live session). The 30-wide proof sets the env override on one host if RAM permits; otherwise the
  second host runs `WORKER_MODULES=coding_agent` — both are documented in `closure_result.md` with the
  measured ceiling.
- **Dead-worker detection** is Conductor's response-timeout lease, which this repository sets to 0
  by policy (`AGENTS.md` timeout policy). Recovery is therefore operator-initiated (supervisor restart
  + workflow retry), and WP2's checkpoints bound the loss to `checkpointEveryTurns` turns.
- **Linux sandboxed checks need `bubblewrap`** (`apt install bubblewrap`); macOS uses the built-in
  `sandbox-exec`. The check reports `sandbox_unavailable` (a named mechanism) rather than running
  unconfined when `sandboxChecks=true` and the tool is absent.
- **Codex/Gemini backends** expose no queued-message/interrupt API; for them feedback lands at the
  next resume boundary (WP2). Claude sessions get turn-level latency.
- **Postgres** is optional at small scale; the file backend remains the default for local runs and
  tests. The DSN can point at the Conductor server's Postgres.

---

## 5a. Implementation notes — where the build departed from this design, and why

Each departure was forced by evidence (a measured failure or an API fact), never by convenience.

- **WP2 — hooks, not a streaming client.** The SDK's `PostToolUse` hook can inject `additionalContext`
  into the running session and any hook can end it (`continue: false`), so in-session feedback needs no
  change of session mode and keeps the durable one-task-per-session path. A `Stop` hook re-polls when
  the agent tries to finish (bounded to 3 re-entries): run `87381848` showed a stop posted 3.5 s before
  the end of a session landing in its final, tool-free turn, where a tool hook never fires.
- **WP2 — checkpoints are snapshot refs, not wip commits.** Committing on the agent's branch would make
  the delivery audit see a clean tree and launch a repair agent. Snapshots are commits built through a
  private index and stored under `refs/cc-checkpoints/<branch>` — branch, index and working tree are
  untouched, and integration needs no squash.
- **WP2 — interrupted sessions skip delivery repair.** `delivery_audit` reports `interrupted`, and
  `code_subtask` routes that past its repair loop: a repair agent would finish what a human stopped.
- **WP1 — the reviewer sees the item's directives.** Run `b9446263` showed a reviewer, blind to steering,
  asking to undo exactly what a human had directed. Reviewers now get the item's directives, human first,
  and are told a human directive overrides the description.
- **WP1 — the final team gate runs `test_cycle` in `full` mode.** In targeted mode a run that integrated
  nothing had "no changed paths to verify" and was mislabelled not-green (run `2c6ca1d1`); an integrated
  branch of several items is verified by the whole documented suite.
- **WP1 — one blocked escalation per blocked spell.** Re-escalating an item the human already resolved
  would re-arm the human gate forever.
- **WP3 — replan loops read a captured variable.** A DO_WHILE's own condition does not resolve a task ref
  from inside the loop (`route__1`), so the loop never re-planned (run `f6015e75`).
- **WP5 — a cross-process lock instead of `update-ref` CAS.** Child teams share one program worktree, so
  they share a filesystem; rebase-onto-head + fast-forward under `git._repo_lock` gives each integrator
  the head the previous one left, which is the CAS guarantee with less machinery.
- **WP6 — authors write in isolated worktrees.** Concurrent authors in one worktree reverted each other's
  files at their write-scope check (run `25fe604e`); `team_acceptance_collect` gathers the files and
  replaces the generic `commit` step, so the task count is unchanged.
- **WP7 — pushback must name a directive the agent was given.** Run `484c7a00` showed an implementer
  using pushback to report that it could not run the tests, which blocked a correct delivery. A pushback
  now counts only when it matches a directive in that item's `dispatch` record.
- **WP9 — loopback only on the remote side.** A Seatbelt `(local ip "localhost:*")` filter matches every
  socket's local side and re-opened the internet (measured); the profile allows outbound only to a
  loopback remote, plus loopback bind and inbound.
- **Scale — 16 concurrent coding sessions per host.** `polling.worker_thread_count` caps a task's threads
  at 16, and `main.py`'s singleton guard refuses a second `coding_agent` deployment on one host. A wider
  wave queues gracefully (H6); a second host running `WORKER_MODULES=coding_agent` adds 16 more.

## 6. Task checklist (tracked here until the OpenSpec change is cut)

- [x] WP1.1 review cap → quarantine; `unavailable` verdict; monitor open-only; console `accept`
- [x] WP1.2 reviewer/planner/test spend recorded; per-role report
- [x] WP1.3 contract controlled event: exception, approve/reject, dependents re-planned, churn cap
- [x] WP1.4 starvation exit + `human_decision` WAIT; outcome mechanisms
- [x] WP4 incremental fold cache; scandir listing; Postgres backend; single ownership fold
- [x] WP2 streaming session + turn-boundary feedback + interrupt; checkpoints; squash integrate
- [x] WP3 `decomposition.py`; three-mode router; replan loops; `bestOf` + `pick_best`; Gate 0 runs
- [x] WP6 acceptance-test authoring + red gate + frozen; per-item `testCommands`; reviewer test audit
- [x] WP5 program board + rank fork + child teams; program-scoped ownership; CAS integrate; team tree; scale 30
- [x] WP7 actor registry + bearer auth; directive lifecycle; `over_constrained`; pushback
- [x] WP8 agent lessons (opinion cap); retraction; in-run + symbol decay
- [x] WP9 sandboxed checks + `runtime_health` proof
- [x] WP10 `dispatch` provenance + trace view; `shapes.py`; `closure_result.md`; roadmap + archive
