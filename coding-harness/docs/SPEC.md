# conductor-code — High-Level Spec (Approved)

> This is the agreed *what/why*. The *how* lives in [`DESIGN.md`](DESIGN.md).

## Context

A coding harness for serious work — new features, updates to existing features,
major bug fixes, and proactive bug hunting — that replaces ad-hoc "vibe coding"
with a documented, spec-first workflow. It ships as a **Claude Code skill/plugin
named `conductor-code`** exposing a small set of slash commands, uses the **Orkes
Conductor engine** to orchestrate each run, and delegates the actual coding to **CLI
coding agents (Claude Code, Codex) wrapped as durable workers** — not raw LLM calls.

Two existing frameworks each do half the job:

- **GitHub Spec-Kit** — strong guardrails (project *constitution*, sequential
  phases, verification gates) but greenfield-leaning and **feature-only**.
- **OpenSpec** — brownfield-first (*living* source-of-truth specs + *change
  deltas* + archive) with **bug-fixes first-class**, but lighter on gates.

`conductor-code` is **not a third spec format**. It sits on top of both: it
**detects and adopts** whichever framework a repo already uses (including specs
authored by others), can **create** specs in either format, and drives a
consistent `explore → spec → plan → implement → verify → ship` lifecycle with
key-checkpoint human approval. Target = **brownfield existing repos**.

## Confirmed decisions

| Decision | Choice |
|---|---|
| Runtime | Claude Code skill (`conductor-code:*`) **driving the Orkes Conductor engine** (engine included in v1). |
| Spec model | Support **both** Spec-Kit and OpenSpec; create new *or* reuse existing. |
| Capabilities | `feature` + `fix` + `find-bug` + shared `explore`/`verify` phases. |
| Gating (HITL) | Approve at key checkpoints (after spec/proposal; before implement) via Conductor **`HUMAN` tasks** that notify and collect structured input; dynamic escalations use the same mechanism to *ask* the human. |
| Process sizing | **Change profiles** pick which optional phases run per change (trivial/standard/full) — completeness without bureaucracy. |
| Safety | **Cross-cutting policies** (guardrails, budgets, escalation, reproducibility) wrap every phase. |
| Agents & models | **Coding is done by CLI coding agents** (Claude Code, Codex) wrapped as workers — *not* raw LLM calls. **Phase-aware**: heavy reasoning (spec/design/plan) on a large model (Opus, max thinking); execution on cheaper/faster agents/models; **cross-agent** review for diversity. Configurable; cost-aware with agent/model escalation. |
| Execution | For large changes, a **planner decomposes the approved plan into a dependency DAG of work-groups** run as **parallel sub-agents in dependency-ordered waves**, each isolated in a worktree and integrated per wave; profile-gated. A **walking skeleton** validates the architecture before fan-out. |
| Verification | **Deep + pluggable + on a real env**: tiered checks (race, integration, property, chaos, linearizability, load) feed the hill-climb score and validate declared **system invariants**; flake-aware. |
| Distribution | `conductor-oss/conductor-skills` marketplace. |

## Command surface

| Command | Purpose |
|---|---|
| `conductor-code:adopt` | Detect/initialize the spec framework + register Conductor defs + start local worker. Idempotent. |
| `conductor-code:feature <desc>` | New feature or update to an existing one. Full lifecycle. |
| `conductor-code:fix <desc \| issue-ref>` | Major bug fix as a first-class change type. |
| `conductor-code:find-bug [scope]` | Read-only audit; ranked report; optional fix proposals. |
| `conductor-code:explore <topic>` | Shared no-stakes investigation front-end. |
| `conductor-code:verify [change]` | Shared converge/verify back-end (tests, types, lint, build, security, review, acceptance). |
| `conductor-code:status` | Show in-flight changes, their phase, pending gates, and budget usage. |

Phases below (clarify, design, document, examples, integrate, …) are **internal to
the workflows** and selected by the change profile — they are not separate
commands in v1.

## Lifecycle backbone

Phases in `[brackets]` are **optional** and switched on/off by the change profile.

```
explore
  → [clarify]        resolve ambiguity / open questions
  → specify          intent + requirements + acceptance criteria
  ── GATE 1: approve spec ───────────────────────────────────────
  → plan             approach + [design/contracts + data-model]
                     + test strategy + risk & rollback
  → tasks            ordered, checkable steps
  → [analyze]        consistency / drift check: spec ↔ plan ↔ tasks
  ── GATE 2: approve plan + design + invariants (before ANY edits) ──
  → [skeleton]       thin end-to-end slice validated vs invariants on a real env (arch changes)
  → implement        code + tests, per repo conventions
  → verify           tiered checks on a real env: tests · types · lint · build · race ·
                     integration · property · chaos · linearizability · load ·
                     security · code review · acceptance · invariants
  → [document]       docs / README / API docs / changelog / ADRs
  → [examples]       usage examples / quickstart / demos
  → [integrate]      branch · conventional commits · PR + description
                     · link issue · wait on CI
  → archive          merge deltas → source-of-truth specs
                     + capture learnings back into the constitution
```

- **GATE 1** — after the spec/proposal is written.
- **GATE 2** — after plan + tasks, before touching the working tree.
- A project **constitution**, if present, is consulted in every phase; `archive`
  feeds new learnings/ADRs back into it so the harness compounds over time.

## Change profiles (right-sizing)

Not every change needs every phase. The harness picks a profile from the change
type + size (overridable), so a typo never treks through design→docs→examples.

| Profile | Typical use | Optional phases on | Gates |
|---|---|---|---|
| **trivial** | typo, comment, tiny config | none (explore → implement → verify → integrate) | auto / single light gate |
| **standard** | most fixes, small features | document, integrate | GATE 1 + GATE 2 |
| **full** | new feature, API/schema change, major fix | clarify, design/contracts, analyze, document, examples, integrate | GATE 1 + GATE 2 (+ optional pre-archive sign-off) |

## Parallel execution (decompose & fan out)

For large changes, implementation (and bug-finding) is **distributed across parallel
sub-agents**. After the plan is approved, the work runs as a **dependency DAG of
work-groups**; groups whose dependencies are met run **in parallel as
dynamically-spawned sub-agents**, in dependency-ordered **waves**:

1. **Decompose** (planner, LLM) → groups with `dependsOn` + **write-scope** (the files
   each owns) + the shared **contracts** they must honor. Folded into the plan, so the
   parallel structure is **approved at GATE 2**.
2. **Fan out** each wave's ready, write-disjoint groups as parallel sub-agents, each in
   its own git worktree.
3. **Integrate** — merge the groups' branches; an agent resolves any residual conflicts.
4. **Verify the wave**, then proceed to the next; **re-plan** remaining work on failure.

**Profile-gated** — only `full`/large changes parallelize; smaller ones implement
sequentially with a single agent. The same `decompose → fan-out → integrate` primitive
powers `find-bug` (parallel scan → dedupe/rank → cross-agent verify), and the `design`
phase's contracts are the **integration seam** that lets independently-built pieces fit.
For architecture-bearing changes, a **walking skeleton** — a thin end-to-end slice
validated against the system invariants on a real env — must pass *before* any fan-out,
so work never parallelizes against a wrong design.

## Review & self-improvement (coding ↔ review loop)

Code isn't trusted just because tests pass — a **reviewer agent** validates it against
the spec/design, at two altitudes:

1. **Inside every sub-agent** — implementation is a **hill-climbing `build ↔ review`
   loop**: each round the builder revises toward the biggest deficit, then the round is
   **scored** (tests + checks + reviewer cleanliness + acceptance); only **improving**
   moves are kept (regressions reverted), and a **reviewer (a *different* agent)** must
   sign off. When the score **plateaus**, the **escalation ladder** promotes the builder
   to a stronger model with more budget — and ultimately a human — before giving up.
2. **After all sub-agents** — a **final review** validates the *whole integrated
   change* against the full spec/design (cross-group integration; gaps an individual
   group reviewer couldn't see). A blocking finding **bounces work back** into a fix
   pass before archive.

Reviews use **cross-agent diversity** (reviewer ≠ builder) so one agent's blind spots
are caught by another.

## Cross-cutting policies (apply to every phase)

These are what make autonomous coding safe and trustworthy:

- **Guardrails** — protected paths (no edits to e.g. `infra/`, CI config,
  lockfiles without an explicit override); secrets are never written or printed;
  no destructive commands (`rm -rf`, force-push, DB drops) without approval;
  blast-radius cap (max files/lines before escalation).
- **Budgets & limits** — per-run caps on iterations, tokens, wall-clock, files
  touched, and $ cost; on breach the run **pauses and escalates** rather than
  grinding on.
- **Confidence-based escalation** — dynamic human-in-the-loop *beyond* the two
  fixed gates: when the agent is uncertain, trips a guardrail, or exceeds a
  budget, it pauses and asks a question instead of guessing.
- **Reproducibility** — every run records model id(s), prompts, tool/SDK
  versions, and the resulting diff/commit in the Conductor audit trail; runs are
  fully inspectable and re-runnable.

## Agent & model policy

**All coding is performed by CLI coding agents** — **Claude Code (`claude`)** and
**Codex (`codex`)** — wrapped as workers, *not* raw LLM calls. Each agent runs its
own internal plan→edit→test loop; the harness orchestrates across invocations. Work
is assigned by **role**, mapping to an **(agent, model, effort)** — front-load
expensive reasoning on a large model, run execution on cheaper/faster agents.

| Role | Default agent · model | Used for |
|---|---|---|
| **architect** | Claude Code · Opus, max thinking | clarify · spec · design · plan · root-cause · analyze · acceptance |
| **builder** | Claude Code · Sonnet *(or Codex)* | implement · reproduce · regression test · bug scan |
| **reviewer** | the *other* agent than built | code review · adversarial verify (cross-agent diversity) |
| **scribe** | Claude Code · Haiku | docs · examples · changelog · commit messages |

- **Configurable** — roles → (agent, model, effort) live in
  `.conductor-code/models.json`, overridable per repo, per profile, and per run.
- **Cross-agent diversity** — review/verify run on a *different* agent than
  implemented, catching agent-specific blind spots.
- **Escalation ladder** — start cheap (OSS/Sonnet); promote (Opus·high → Opus·max +
  more budget, optionally swap agent) when not converging — tests stay red *or* the
  **reviewer** keeps finding issues — then a human gate as the last rung.
- **Cost-aware** — the budget tracker prices per agent/model.
- **One config file** — agents, tiers, role→tier + ladders, per-profile overrides, the
  `reviewLoop` hill-climb (score weights, `target`, `plateau`), budgets, and prices live
  in `.conductor-code/models.json` (see [`docs/config/models.example.json`](config/models.example.json)).

## Common change model (format-agnostic)

```
intent         why this change exists (problem / goal)
requirements   what — the spec / scenarios + acceptance criteria
design         contracts / data-model / interfaces — the parallelization seam   [optional]
invariants     system properties + the checks that prove them (e.g. linearizable writes)
plan           approach + test strategy + risk & rollback
tasks          dependency DAG: dependsOn + write-scope per group (the parallel plan)
deltas         impact on existing specs: ADDED / MODIFIED / REMOVED
artifacts      what was produced: code + tests + docs + examples
verification   tiered checks (tests · race · integration · property · chaos · linearizability · load) · review · acceptance · invariants
integration    branch · commits · PR · linked issues · CI status   [optional]
decisions      ADRs / learnings captured back into the specs
budget         iterations / tokens / time / cost used vs caps
```

## Verification (deep, pluggable, on a real environment)

A green unit build isn't trust. The verification suite is **pluggable and tiered**
(configured in `checks.json`), feeds the hill-climb **score**, and runs against a
**real provisioned environment** — so it catches the bugs that matter for concurrent and
distributed code, not just compile errors:

- **Kinds** — tests (new + regression) · type-check · lint · build · **race/concurrency**
  · **integration** · **property-based** · **consistency / linearizability** ·
  **fault-injection / chaos** · **load/soak** · security · **code review** (cross-agent,
  + optional `dg`) · acceptance · **consistency/`analyze`** (code↔plan↔spec).
- **Tiered by cost** — cheap deterministic checks run **per-round** in the group
  hill-climb; integration/property **per-wave**; expensive distributed checks
  (chaos/linearizability/load) only at the **final** review.
- **Real environment** — `needsEnv` checks run against an N-node cluster the harness
  stands up and tears down.
- **System invariants** — explicit properties (e.g. linearizable writes,
  no-loss-on-failure) are declared in the design, mapped to the checks that prove them,
  and validated at the skeleton, per-wave, and final review.
- **Flake-aware** — non-deterministic checks are re-run and scored by pass-rate, so
  flaky results can't fool keep-best or plateau detection.

It doubles as the **final review** against the spec/design + invariants — a blocking
finding bounces work back (an invariant failure all the way to design), not just fails.
Schema: [`docs/config/checks.example.json`](config/checks.example.json).

## The three workflows

- **`feature`** — full profile by default. New capability → new spec; update →
  delta against the existing spec. Honors the constitution; clarifies ambiguities
  before GATE 1; produces code **and** tests, docs, examples, and a PR.
- **`fix`** — standard/full by severity. `reproduce → root-cause → minimal-change
  proposal → GATE 1 → failing regression test → implement → verify → [document] →
  integrate → archive`. First-class change type with severity metadata.
- **`find-bug`** — read-only (guardrail-enforced). `scope → multi-lens scan
  (correctness / security / perf / concurrency) → adversarial verification →
  ranked report`, with opt-in fix-proposal generation behind a gate; each accepted
  finding becomes a `fix` run.
