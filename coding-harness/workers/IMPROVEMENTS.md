# Coding Harness — Improvement Design & Implementation Plan

**Goal:** evolve the harness from "builds one small feature reliably" to "builds large,
complex production systems." This doc captures the design, concrete implementation
details, and todos for four phases. Phases 1–2 harden and simplify the existing engine;
Phases 3–4 add the structural pieces required for scale.

**Status legend:** ✅ done · 🚧 in progress · ⬜ todo

---

## 0. Current architecture (baseline)

**Workers** (`conductor-python`, `@worker_task`; driven by the Claude Agent SDK):
- `claude_code` — edits scoped files in a worktree (session-resume aware, turn-capped, guardrail-enforced)
- `code_review` — SDK reviewer (reads the worktree)
- `read_worktree` — cheap file reader for the LLM review path
- `test` — deterministic test runner (+ optional agent diagnosis)
- `ui_test` — Playwright; static fallback in-loop, interactive create-flow + screenshots post-integration
- gitops: `create_branch`, `commit`, `worktree_add`, `worktree_remove`, `merge_worktrees`, `keep_best`, `push`/`pull` (stubs)
- pipeline: `prepare`, `planner`, `archive`

**Workflows:** `cc_feature` (main, 29 tasks) → `cc_design_loop` (design⇄review hill-climb) →
`cc_implementation_loop` (per-group: claude_code → test|ui_test → hybrid review → keep_best).

**Shared (`common/`):** `config`, `claude` (SDK wrapper), `git`, `scoring`, `policy`, `cost`,
`payloads`, `results`, `exec`.

**Principles today:** orchestration state in workflow variables; code in git worktrees;
contracts-first wave decomposition (FORK_JOIN_DYNAMIC per wave, 3-wave ceiling); hill-climb
scoring (checks + review) with model-ladder escalation; hybrid review; session resume.

**Known ceiling:** ~1 feature / ~10 files / 4–8 groups / 3 waves per run.

---

## Phase 1 — Correctness (fix the lies)  ✅ DONE

Bugs where the harness reported success it hadn't earned.

### 1.1 `test` runs shell strings without a shell ✅
- **Was:** `subprocess(shlex.split(cmd))` — `node --check a.mjs b.mjs && echo ok` split `&&`
  into an argv token, so only the first file was checked and `&& echo ok` was swallowed.
- **Fix:** run via `["bash","-lc", cmd]`. Also fixed an `UnboundLocalError` on the timeout
  path (`res.code` referenced when `res` never bound → now `exit_code` sentinel).
- **File:** `test/tasks.py`.

### 1.2 Commit hygiene ✅
- **Was:** `archive`/`commit` do `git add -A`; runtime files (`.cc-input.json`, `.wf_id`,
  `.start`, `.cc-artifacts/…png`) landed in commits. The TS `adopt` seeded `.gitignore`; the
  Python port dropped it.
- **Fix:** `prepare` idempotently seeds `.gitignore` (`_seed_gitignore`, `GITIGNORE_ENTRIES`)
  with `.cc-worktrees/ .cc-artifacts/ .conductor-code/ .cc-input.json .wf_id .start
  node_modules/ *.db*`.
- **File:** `pipeline/tasks.py`.

### 1.3 Scoped-mode score masked failing checks ✅
- **Was:** in scoped mode `impl_score` returned `1.0` on review approval alone → a group whose
  syntax check failed could reach `done=true`.
- **Fix:** one formula in both modes: `0.6*checks + 0.4*review`; `done` (≥1.0) now always
  requires the check to pass. `is_full_suite` retained for signature compat (no longer changes math).
- **File:** `common/scoring.py`; tests updated.

### 1.4 Ladder had two sources of truth ✅
- **Was:** `keep_best` re-read the repo `models.json` and ignored the `builderLadder`/`plateauRounds`
  passed through the workflow (resolved once by `prepare`). Drift silently broke escalation.
- **Fix:** `keep_best` uses the passed ladder/plateau; repo config is fallback only.
- **File:** `gitops/tasks.py`.

**Verification:** 24 unit tests green; `.gitignore` idempotency + shell `&&` semantics smoke-tested.

---

## Phase 2 — Simplify (delete the bug-prone surfaces)  🚧

Two surfaces caused most of our iteration failures: (a) three copy-pasted wave blocks with
nested SWITCHes in `cc_feature.json`, (b) embedded graaljs INLINE expressions (the `${`
corruption, quote bugs) and regex JSON extraction (invalid-escape crashes). Remove both.

### 2.1 Collapse the 3 hard-coded waves into an N-wave loop
- **Was:** `cc_feature.json` hand-codes wave0 inline + `wave1_gate`/`wave2_gate` SWITCHes, each
  a near-duplicate (fan_out → join → agg → merge → verify). ~40% of the file; hard ceiling of 3 waves.
- **Design (primary): recursive `cc_wave` sub-workflow.** A single sub-workflow processes one
  wave, then a SWITCH conditionally invokes `cc_wave(index+1)` when more waves remain. Handles
  arbitrary wave counts (required at scale) and exists once.
  - `planner` already emits `waves: [[ids...], ...]`; add `waveCount` and expose each wave's
    `{tasks, inputs, groupIds}` addressable by index (list, not `wave0/1/2` fields).
  - `cc_wave` inputs: `repoPath, waveIndex, waveCount, waveTasks, waveInputs, waveGroupIds,
    nextWave…, modelBuilder`. Body: FORK_JOIN_DYNAMIC(waveTasks/waveInputs) → JOIN → agg(INLINE
    or worker) → `merge_worktrees(waveGroupIds)` → wave gate (P3.2) → SWITCH `hasNext` →
    SUB_WORKFLOW `cc_wave(waveIndex+1)`.
  - `cc_feature` calls `cc_wave(0)` once; deletes wave0/1/2 blocks.
- **Fallback:** if recursion or DO_WHILE-with-dynamic-fork proves unsupported in this Conductor
  build, generate the bounded wave blocks programmatically (planner emits the exact task list;
  `cc_feature` becomes a thin template) — still removes the copy-paste, keeps a (higher) ceiling.
- **Feasibility gate:** before committing, confirm a sub-workflow can recursively invoke itself
  and that a JOIN inside it aggregates dynamic forks (spike with a trivial 2-level workflow).
- **Files:** `pipeline/tasks.py` (planner output shape), new `workflows/cc_wave.json`,
  `workflows/cc_feature.json` (gut the wave blocks), `workflows/taskdefs/`.

### 2.2 SDK structured outputs → delete JSON-parsing hacks
- **Was:** `planner._extract_json` regex-extracts + repairs invalid `\escape`s (crashed once);
  `code_review._parse` has a JSON fallback. Both fragile.
- **Design:** the Agent SDK returns `ResultMessage.structured_output`; `run_agent` already
  surfaces it as `res["structured"]`. Pass an `output schema` (via `ClaudeAgentOptions` /
  system-prompt JSON instruction) for `planner` and `code_review`; consume `structured` first,
  fall back to text-parse only if absent.
  - `common/claude.py`: add optional `output_schema` / `structured=True` handling; ensure
    `structured` is populated.
  - `pipeline/tasks.py planner`: consume `res["structured"]["groups"]`; keep `_extract_json`
    only as fallback.
  - `code_review/tasks.py`: consume `res["structured"]`; drop the regex path.
- **Removes:** the escape-repair regex and its whole failure class.

### 2.3 Consolidate loop INLINEs into `keep_best`
- **Was:** each `cc_implementation_loop` iteration runs 4 graaljs INLINEs (`review_gate`,
  `score_step`, `format_feedback`, `next_resume`) + `accumulate_cost`. graaljs caused 2 failures.
- **Design:** fold `score_step`, `format_feedback`, `next_resume`, `accumulate_cost`, and the
  `review_gate` escalate decision into the `keep_best` **Python** worker (it already receives
  scores, review output, cost, session id, stall/tier). It returns `{done, exhausted, bestScore,
  bestCommit, stall, tierIdx, curModel, feedback, totalCostUsd, totalTokens, resumeSessionId,
  escalateReview}`. The loop keeps only: `claude_code → test_router → review_router → keep_best →
  update_group_state(SET_VARIABLE)`.
  - `review_gate` becomes tricky: `escalateReview` is needed *before* the review runs, but
    `keep_best` runs *after*. Resolve by computing next-iteration `escalateReview` in `keep_best`
    and storing it in state (init `false`); `review_router` reads `${workflow.variables.escalateReview}`.
  - Net: 5 INLINEs → 0 in the loop; all logic unit-testable Python.
- **Files:** `gitops/tasks.py keep_best`, `workflows/cc_implementation_loop.json`, `tests/`.

### 2.4 Remove/repurpose dead per-wave verifies
- **Was:** `verify_wave0/1/2` run the full suite against stub-filled trees, always fail, output
  consumed by nothing — pure wasted latency/tokens.
- **Design:** delete them here; re-introduce a *real* wave gate in P3.2 (build-level check after
  each merge that actually gates progression).

**Phase 2 verification:** feasibility spike for 2.1; re-register; `pytest` green; one full
`cc_feature` run on pinboard matches iter13 behavior (pass, ~$1.2, screenshots) with the
simplified graph. Diff should be net-negative lines.

---

## Phase 3 — Scale foundations  ⬜

The pieces that make a *single* `cc_feature` survive a real-sized change.

### 3.1 Artifacts on the branch (not in workflow variables)
- **Problem (verified bug #5):** planner duplicates `contracts`+`specText` (~24KB each) into
  *every* group input (8 groups → ~200KB); large designs blow Conductor's payload cap. Design
  as a workflow variable is a guaranteed failure at scale.
- **Design — revises the earlier "no external files" rule** (confirmed with user): keep
  *orchestration state* (scores, flags, session ids, counters) in workflow variables, but write
  **artifacts** to the change dir on the branch, and pass **paths**:
  - After `create_branch`, a `write_artifacts` worker writes `openspec/changes/<id>/{proposal,
    design,contracts,test-spec,tasks}.md` and commits them on the change branch.
  - `worktree_add` already copies tracked files into each group worktree → agents read
    `contracts.md`/`design.md` **natively from disk** (cheaper + higher-fidelity than prompt
    injection — it's how Claude Code is designed to work).
  - `planner`/`claude_code`/`code_review` inputs carry `contractsPath`/`specPath`, not text.
    `claude_code` prompt: "read `./openspec/changes/<id>/contracts.md`" instead of inlining.
  - `payloads.slice_var` injection shrinks to a short excerpt or disappears.
- **Wins:** no payload limit, ~10× smaller planner output, artifacts versioned with code, cheaper
  agent context. **Files:** new `pipeline` `write_artifacts` (or extend `archive`), `planner`
  output, `claude_code`/`code_review` prompts, `cc_feature.json` ordering (write artifacts right
  after branch), `cc_implementation_loop.json` inputs.

### 3.2 Failure localization + real wave gates
- **Problem (bugs #6, #8):** `final_verify` says "failed" with no locality; the fix fan-out
  re-runs *wave1* regardless of which group owns the failure and passes no failure detail.
- **Design:**
  - `test` worker: parse the runner's failing-test list → file paths (Node: `node:test` TAP/loc;
    generalize per language in P4.1). Return `failingFiles: [...]`.
  - New `map_failures` helper (pure, `common/`): given `failingFiles` + the planner's file→group
    map, return the set of implicated group ids.
  - `archive_gate` fix pass: re-run only the implicated groups (dynamic fork over that subset),
    passing the specific failure output as `feedback`. If mapping is empty/ambiguous, fall back
    to all final-wave groups.
  - **Real wave gate:** after each wave merge, run a *build/smoke* check (not the full suite):
    e.g. `node --check` across changed files + import smoke. If it fails, localize + fix before
    proceeding to the next wave (fail fast, cheaper than discovering at final_verify).
- **Files:** `test/tasks.py`, new `common/localize.py`, `cc_wave.json`/`cc_feature.json` gate +
  fix wiring, `planner` (persist file→group map to an artifact for the mapper).

### 3.3 Budget enforcement + run report + LLM pricing
- **Problem (bug #7 + no governance):** `policy.json perRun.maxCostUsd:50` is declared but never
  enforced; server-side LLM tasks report `costUsd:0` (no pricing); merge tokens counted only for
  wave0.
- **Design:**
  - `common/cost.py`: add a price table (USD/1M in + out per model, from `models.json prices`) and
    `price(tokens_or_usage, model)`; apply to LLM-task `tokenUsed` so `architect`/`design`/`review`
    show real cost.
  - **Budget gate:** after each wave's cost aggregation, compare cumulative cost to
    `policy.budgets.perRun.maxCostUsd`; on breach → HUMAN gate (approve-to-continue / abort).
    Also cap iterations via `perGroup.maxRounds`.
  - **Run report:** a `run_report` worker writes `openspec/changes/<id>/report.md` (cost breakdown,
    per-group scores/tiers, wave outcomes, screenshot paths, final verdict) and it's surfaced in
    workflow output + committed. Answers "what did this run do and cost?" in one artifact.
- **Files:** `common/cost.py`, new `pipeline` `run_report`, `cc_feature.json` (budget gate +
  report), `defaults/models.defaults.json prices`.

---

## Phase 4 — Large, complex systems  ⬜

Where "really large" actually happens. C1 is the headline; the rest support real-world breadth.

### 4.1 Hierarchical decomposition: `cc_program`
- **Problem:** single-level decomposition (one planner → 4–8 groups) caps at one feature. A real
  system (30+ modules, multiple services, migrations, infra, UI) needs a level above `cc_feature`.
- **Design — `cc_program` workflow:**
  1. `program_prepare` + `program_design` (LLM/agent): from the system spec, produce a
     **program-level design + cross-subsystem contracts** (shared interfaces, data schemas, API
     boundaries) and a list of **subsystems** with `dependsOn` (e.g. `db`, `api`, `worker`, `auth`,
     `ui`). Written as artifacts on a program branch.
  2. HUMAN gate: approve program decomposition + shared contracts.
  3. **Topologically ordered milestones:** for each subsystem (respecting `dependsOn`), invoke
     `cc_feature` as a SUB_WORKFLOW, scoped to that subsystem's spec slice, **sharing the program
     branch and shared contracts**. Independent subsystems in the same topological rank can fan out
     in parallel (FORK_JOIN_DYNAMIC over `cc_feature` instances); dependent ranks run in sequence.
  4. **Integration verify between milestones:** after each rank merges, run the growing
     cross-subsystem integration suite; localize+fix failures at the program level before the next
     rank.
  5. `program_report` aggregates per-subsystem reports + total cost.
- **Reuses:** the entire `cc_feature`/`cc_wave`/`cc_implementation_loop` stack unchanged — this is
  a composition layer. Recursion depth is bounded (program → feature → wave → group loop).
- **Files:** new `workflows/cc_program.json`, new `pipeline` workers `program_decompose` /
  `program_report`, `defaults/` program config, taskdefs.

### 4.2 gateMode (human | auto) for unattended runs
- **Problem:** HUMAN gates block scheduled/unattended (`cc_program` with 10 subsystems ×
  3 gates = unusable unattended).
- **Design:** a `gateMode` input threaded through all workflows. `human` → HUMAN task (today).
  `auto` → the gate is a SWITCH that auto-passes (records the decision in the report) — for
  trusted/low-risk runs and scheduling. Implemented as a small `gate` sub-workflow wrapping the
  HUMAN task in a `gateMode` SWITCH so every gate site is one line.
- **Files:** new `workflows/cc_gate.json`, all workflow gate sites, README scheduling section.

### 4.3 Polyglot support (drive everything from `checks.json`)
- **Problem:** scoped checks, test-file names, and worktree file-copy are hardcoded Node/Go
  (`planner._scoped_cmd`, `git.worktree_add` copies `test/`+`package.json`).
- **Design:** move language specifics into `checks.json` / `models.json`:
  `{ lang, scopedCheckCmd, testCmd, buildCmd, worktreeCopyGlobs, uiStartCmd }`. `prepare` resolves
  them; `planner`/`git`/`test`/`ui_test` consume config instead of branching on `lang`. Ship
  presets for node, python, go, rust, java.
- **Files:** `common/config.py`, `pipeline/tasks.py`, `common/git.py`, `defaults/checks.defaults.json`.

### 4.4 Spec-driven UI scenarios + house style (fixes bug #9 + UI inconsistency)
- **Problem:** `ui_test` hard gate assumes a CRUD create-form (a read-only dashboard fails "no
  interactive form"); and UI quality varies run-to-run because terse specs don't demand UX.
- **Design:**
  - **Scenarios from the test-spec:** the architect emits UI interaction scenarios (steps +
    expected visible outcome) into `test-spec.md`; `ui_test` executes those instead of the generic
    form heuristic. No scenarios → fall back to load-check only (no false CRUD failure).
  - **House style:** inject standing UI/UX guidance into the `claude_code` prompt for
    `testKind=ui` groups (centered container, list/table for collections, loading/empty/error
    states, labelled inputs + focus-visible, no external CDNs). Consistent quality regardless of
    spec terseness.
- **Files:** `ui_test/tasks.py`, `pipeline/tasks.py` (architect prompt for UI scenarios),
  `claude_code/tasks.py` (UI house-style block).

---

## Cross-cutting todos

- ⬜ Feasibility spike: recursive sub-workflow + JOIN-of-dynamic-fork (gates 2.1 & 4.1 design).
- ⬜ Keep `pytest tests/` green after every phase; add tests for `map_failures`, cost pricing,
  gate/gateMode, program decomposition (pure parts).
- ⬜ After each phase: `register.sh`, supervised poller reload, one regression `cc_feature` run on
  pinboard (must stay pass / ~$1.2 / screenshots) before moving on.
- ⬜ Update `README.md` (workflow map, `cc_program`, gateMode, polyglot config) as phases land.

## Acceptance (Phase-4 exit)

1. Regression: `cc_feature` on pinboard still passes first-go, cost within ~10% of $1.2, 3 UI
   screenshots incl. populated state, clean commit (no cruft).
2. Scale: a new **multi-subsystem** spec (e.g. "taskboard": `db` + JSON `api` + background `worker`
   + `ui`, with shared contracts) built via `cc_program` — subsystems decomposed, built in
   dependency order (independent ones in parallel), cross-subsystem integration suite green,
   `program_report.md` produced with total cost.
3. Independent verification: check out the program branch, run the full test suite + boot the app +
   Playwright the UI; no out-of-scope cruft; budget respected.

## Sequencing

**P1 (done) → P2** (same files, delete risk) **→ P3** (scale a single feature) **→ P4** (compose
into programs). P4.1 `cc_program` is the unlock, but it stands on P3's artifacts-on-branch,
localization, and budget governance — building it earlier would be on sand.
