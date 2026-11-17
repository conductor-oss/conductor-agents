## Context

The `issue_to_pr` workflow resolves GitHub issues autonomously: it fetches an
issue, clones the repo, runs `code_parallel` (which plans with OpenSpec and then
fans out parallel coding agents), and opens a PR. Because the planner never sees
the GitHub issue directly — only the text the harness forwards to it — the
fidelity of that hand-off determines whether the generated plan matches what was
actually requested. If the issue body is silently truncated, escaped, or mangled
between the GitHub API and `openspec new change`, the planner works from an
incomplete prompt with **no visible error**, a failure class that is hard to
notice after the fact.

This change is an **investigation**, not a code change. The deliverable is a
written report that traces the full issue-text data flow with concrete
`file:line` references and assesses every candidate loss point, plus an OpenSpec
spec (`harness-issue-ingestion`) that captures the required ingestion behavior as
a contract for any future hardening. The proposal (`proposal.md`) states the
motivation and the summary of findings; this document explains *how* the
investigation was conducted and *why* the report is structured and scoped the
way it is.

Traced data flow (the ground truth this design rests on):

1. `common/github.py:89-104` — `issue_fetch()` runs
   `gh issue view <n> --repo <slug> --json number,title,body,state,url,labels`
   and returns `body` **verbatim** (`d.get("body", "")`). No length cap.
2. `gitops/tasks.py:216-226` — the `issue_fetch` worker task wraps it; the only
   truncation is cosmetic (`title[:80]`) inside the human-readable log line, not
   on the forwarded payload.
3. `issue_to_pr.json:60` — Conductor template substitution inlines
   `${issue.output.title}` and `${issue.output.body}` into a single `instruction`
   string.
4. `code_parallel.json:65` → `openspec_plan.json` — `instruction` flows through
   unchanged and reaches the planner via **two independent paths**:
   - **Path (a):** `openspec_plan.json:25` sets it as `openspec_new_change`'s
     `description`, which becomes `openspec new change <name> --description <body>`
     — one CLI argument (`openspecops/tasks.py:23` → `common/openspec_cli.py:43-47`).
   - **Path (b):** `openspec_plan.json:41` passes it as `goal` into the
     artifact-drain loop; `openspec_generate_artifact.json:28` inlines `goal`
     directly into each artifact-writer agent's prompt. **This is the text that
     actually drives content generation.**

The stakeholder is whoever operates or extends the harness; the constraint is
read-only investigation — no runtime, API, or dependency changes.

## Goals / Non-Goals

**Goals:**
- Produce a self-contained markdown report that traces the issue title/body from
  `gh` fetch to the OpenSpec planner with a `file:line` reference at every hop.
- Enumerate and classify every candidate loss point (truncation, escaping,
  interpolation, payload-size, arg-length) as either *confirmed hard cap*,
  *possible silent loss*, or *fail-hard (not silent)*.
- Establish the key architectural finding that two independent paths carry the
  instruction, and identify which one is load-bearing for planner content.
- Record actionable recommendations (verification tests / guardrails) scoped so a
  later change can act on them without re-doing the trace.
- Codify the required ingestion behavior in the `harness-issue-ingestion` spec.

**Non-Goals:**
- No source, workflow, or config changes — this change ships analysis only.
- No fix implementation, verification harness, or new ingestion path (those are
  follow-ups the report *recommends*).
- No investigation of behavior *inside* the `openspec` CLI binary itself (it lives
  downstream of this repo); the report may only note it as a suspected loss point
  and recommend a black-box test.
- No changes to how the coding agents or PR creation consume the plan.

## Decisions

**Decision 1 — Deliver a standalone markdown report, not code comments.**
The proposal offered "markdown file or code comments" as options. Choose a single
markdown report. Rationale: the finding spans six files across three
subsystems (`gh`, Conductor templating, `openspec` CLI); a linear narrative with
a hop-by-hop table is far more legible than comments scattered across read-only
files, and — critically — the task forbids code changes, which annotating source
files would violate. *Alternative considered:* inline comments at each hop —
rejected as both a code change and a fragmented reader experience.

**Decision 2 — Structure the report as a hop-by-hop trace table + a
loss-point risk register.** Each hop gets `file:line`, what it does to the text,
and a verdict (verbatim / cosmetic-only / transformed). The loss points get their
own register classified by *silence* (silent-corruption vs. fail-hard), because
the operator's real concern is **silent** loss — a hard failure is already
visible. Rationale: this directly answers the task's three questions
(where fetched, how passed, what could cut it off) and makes the "silent vs.
loud" distinction the organizing principle. *Alternative considered:* a pure prose
walkthrough — rejected as harder to audit against the code.

**Decision 3 — Name the two-path architecture as the central finding.** The
report must make explicit that path (a) (`--description`) and path (b) (`goal`
→ agent prompt) both carry the instruction, and that **path (b) is the one that
feeds the content-generating agent** while path (a) only seeds scaffolding.
Rationale: this reframes the risk. Even if `openspec new change --description`
treats the body as a short summary/first line (the single most likely *silent*
truncation, and one that is invisible here because it is internal to the
downstream CLI), the planner still receives the **full** body through path (b).
The report should therefore rank the `--description` truncation as *lower
operational severity than it first appears* — a nuance a naive trace would miss.
*Alternative considered:* treating `--description` as the sole/primary risk —
rejected because it overstates impact and would misdirect any follow-up fix.

**Decision 4 — Classify OS arg-length and Conductor payload limits as
fail-hard, not silent.** `ARG_MAX` / per-arg `MAX_ARG_STRLEN` on the single
`--description <body>` argument, and Conductor's task input/output size limits,
raise errors rather than silently dropping bytes. Rationale: `exec.run`
(`common/exec.py`) surfaces non-zero exits as `RunError`, and Conductor rejects
oversized payloads loudly. The report keeps these in the register but marks them
as *detectable*, so mitigation is "add a guardrail/size check," not "hunt for
silent corruption." *Alternative considered:* omitting them — rejected because a
very large body is exactly when they trigger and an operator needs to know they
exist.

**Decision 5 — Flag `${...}` re-interpolation and JSON escaping as
*possible-silent* and recommend a test rather than asserting behavior.** An issue
body containing `${...}` or dollar-brace sequences could be re-interpreted by
Conductor template substitution, and quotes/backslashes/newlines pass through
JSON-string escaping during substitution. The report should not *assert* these
corrupt the text (that depends on Conductor internals not owned here) but should
mark them *possible-silent* and recommend a concrete round-trip test. Rationale:
honest reporting — state what the code proves versus what needs empirical
confirmation. *Alternative considered:* declaring them safe/unsafe outright —
rejected as unverifiable from static reading alone.

**Decision 6 — Encode the behavior as a new capability spec, not a modification.**
The issue-fetch → instruction-assembly path is not covered by any existing spec
(`harness-openspec-planning` starts from an already-assembled instruction).
Therefore `harness-issue-ingestion` is a **new** capability; no existing
requirement changes. Rationale: keeps the spec surface accurate and avoids
retrofitting requirements onto a loop that doesn't own ingestion.

## Risks / Trade-offs

- **[The report asserts a loss point the code can't prove]** → Every claim about
  behavior internal to the `openspec` CLI or Conductor engine is labeled
  *possible-silent* and paired with a recommended verification test, never stated
  as fact. Static-only claims (verbatim fetch, cosmetic `title[:80]`) are backed
  by exact `file:line`.
- **[`--description` truncation overstated, misdirecting a future fix]** →
  Decision 3 explicitly ranks it against path (b), so a follow-up fixes the
  load-bearing path, not just the cosmetic one.
- **[Trace drifts from code as the harness evolves]** → Report and spec pin every
  hop to `file:line` so a reader can re-verify quickly and detect drift; the spec
  states the *required behavior* (full body reaches the planner) so it stays valid
  even if line numbers move.
- **[Investigation misread as authorizing a code change]** → The proposal, this
  design, and the report all state "no code changes"; recommendations are
  explicitly deferred to a later change.
- **Trade-off: static-analysis depth vs. empirical proof.** This change stops at a
  traced, reasoned report and defers runtime confirmation (large-body and
  special-character round-trip tests) to a follow-up. Accepted because the value
  is in mapping the terrain and the risk register; running live tests is a
  separate, larger effort with its own environment needs.

## Migration Plan

Not applicable — this change adds documentation (an investigation report) and a
new spec only. There is no deployable artifact, schema, or runtime change, and
therefore nothing to roll back beyond deleting the added markdown files. The
recommendations the report produces would be scoped, designed, and migrated as
their own future change.

## Open Questions

- Does `openspec new change --description <body>` persist the **full** body into
  `proposal.md`, or does it treat it as a one-line summary/title? (Requires a
  black-box test of the downstream CLI; the report recommends this test.)
- Does Conductor re-interpolate `${...}` / dollar-brace sequences that appear
  **inside** an issue body during template substitution, and does JSON escaping
  round-trip newlines/quotes/backslashes losslessly? (Requires a live round-trip
  test with a crafted issue body.)
- What are the effective Conductor task input/output payload-size limits in this
  deployment, and at what body size do they (or OS `ARG_MAX`) trigger — and do
  they fail loudly in all cases as expected?
- Should a future hardening pass a large body via stdin/file to `openspec` (and to
  the agent prompt) instead of a single CLI argument, to sidestep arg-length
  limits entirely? (A recommendation to evaluate, not decided here.)
