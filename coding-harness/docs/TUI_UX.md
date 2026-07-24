# Harness TUI — UX Design

The terminal interface for humans driving the coding harness: kick off work, watch agents
code in real time, answer the questions workflows ask, and manage runs — without writing
JSON or leaving the terminal. (Agents keep using `SKILL.md`; this is the human surface.)

## Principles

1. **Actions, not workflows.** Users pick "Review a PR", not `pr_review`. Workflow names
   appear as secondary detail.
2. **Zero JSON.** Every input is a form field with the registered default shown; every
   output is a rendered card with the thing you want (the PR URL) one keypress away.
3. **Live by default.** Anything RUNNING updates itself (2–5s). The per-turn progress the
   workers already publish (turns, commands, tokens, cost) is rendered, not hidden.
4. **Safe by default.** Destructive actions (terminate) confirm; nothing merges or
   force-pushes from the TUI; HITL responses show exactly what approving will cause.
5. **Keyboard-first, mouse-friendly.** Single-key verbs everywhere, visible in the footer;
   everything also clickable (Textual gives this for free).
6. **Honest about failure.** Workers down, run hung, task failed — each has a distinct,
   explained state with the fix hinted, never a silent spinner.

## The four v1 journeys

| # | Journey | Entry |
|---|---|---|
| 1 | Kick off new work (review / issue→PR / feedback / local change) | `n` anywhere |
| 2 | Monitor running work (fleet view + per-run deep dive) | home screen / `enter` |
| 3 | Terminate / retry / re-run | inside run detail |
| 4 | List, filter, search runs (incl. history) | home screen `/`, `f` |

(Responding to HITL prompts is designed below but **deferred to v2** — see §HITL.)

---

## Screen map

```
                       ┌────────────────┐
        n ────────────▶│  New Run       │──start──▶┐
┌───────────────┐      │  (launcher)    │          │
│  Dashboard    │      └────────────────┘          ▼
│  (home)       │◀──esc── ┌──────────────────────────────┐
│               │──enter─▶│  Run Detail (live)           │
│               │         │   · task tree  · agent panel │
│               │◀──esc── │   · result card · logs       │
└───────────────┘         └──────────────────────────────┘
```

Three screens + one overlay (logs viewer) in v1. No deeper navigation. (v2 adds the HITL
respond overlay.)

---

## 1. Dashboard (home) — the fleet

```
┌ Conductor Coding Harness ─────────── server ✓ · workers: coding_agent ✓ 2s · gitops ✓ 1s ┐
│                                                                                          │
│  ▶ RUNNING    pr_review     0b22b6  acme/app PR#7       02:14   11.5k tok  $0.05        │
│  ▶ RUNNING    code_parallel 4c5fe1  /tmp/cp_oracle      41:02  1.2M tok   $3.41         │
│  ✓ COMPLETED  issue_to_pr   d8e56e  acme/app #3 → PR#4  04:41   54.8k tok  $0.13        │
│  ✗ FAILED     address_pr    f548a9  acme/app PR#4       01:02   terminate/retry →       │
│  … 50 most recent, newest first, live-refresh 5s                                        │
│                                                                                          │
└ [n]ew  [enter] open  [f] running-only  [/] search  [o] browser  [q]uit ──────────────────┘
```

- **Columns**: status glyph+color · workflow · short id · **target** (humanized from the
  run's input: `repo#issue`, `PR#n`, `repoPath`) · elapsed/duration · tokens · cost.
  Tokens/cost are live for RUNNING rows (summed from task snapshots), final for others.
- **Health strip** (always visible): Conductor reachability + last poll time per worker
  module. Red "workers down — runs will hang" banner when stale >15s: this is the #1
  footgun and it's diagnosed at a glance, before you launch.
- **Search/filter**: `/` free-text filters the visible set (target, id, workflow);
  `f` cycles ALL → RUNNING → FAILED; `F` opens a filter panel (workflow type multi-select,
  time window, repo). Sort: newest first (fixed — predictable beats flexible).
- **History depth**: the 50 most recent runs (decided; no date-range paging in v1).
- `o` opens the selected run in the Conductor web UI (escape hatch to full detail).

## 2. New Run (launcher) — kicking off work

**Step 1 — pick the action** (not the workflow):

```
┌ New run ──────────────────────────────────────────────┐
│ ▶ Review a pull request                  (pr_review)   │
│   Resolve a GitHub issue into a PR       (issue_to_pr) │
│   Address review feedback on a PR        (address_pr)  │
│   Code a change in a local repo          (code_parallel)│
└ ↑↓ choose · enter next · esc cancel ──────────────────┘
```

(`github_demo` is hidden from the launcher — it's a plumbing smoke test, still runnable
via the CLI; its runs still appear on the Dashboard.)

**Step 2 — the form.** Required fields first, then common options, **Advanced** collapsed.
Every field shows its default and inline validation. Example (`issue_to_pr`):

Every coding form includes **Keep worktree**. GitHub forms also accept an optional
**Local checkout**; chat maps a mentioned directory to the same `repoPath` input. The path is
expanded before launch and the confirmation shows the source checkout, planned
`.cc-worktrees/run-<workflow-id>` workspace, ignored dirty-change count, and retention choice.

```
┌ Resolve a GitHub issue into a PR ────────────────────────────────┐
│ Repo *        [acme/app_______________]  (URL or owner/name)     │
│ Issue *       [#51 Fix login redirect ▾]  ← live picker via gh   │
│ Base branch   [main____]                                          │
│ Backend       (•) claude  ( ) codex  ( ) gemini                  │
│ Design docs   [ ] generate design docs first (larger changes)    │
│ Parallelism   [4] max sub-tasks                                   │
│ ▸ Advanced (models, turns, budget $50.00)                        │
│                                                                   │
│ Preflight  ✓ server  ✓ workflow registered  ✓ workers polling    │
│                                                    [ Start ▶ ]   │
└ tab fields · enter start · esc back ─────────────────────────────┘
```

- **Pickers over typing**: once Repo is set and `gh` is available locally, Issue/PR fields
  become searchable dropdowns of open issues/PRs (number + title). Without gh: plain
  number input (graceful degrade).
- **Preflight inline, before Start enables**: server ✓ · def registered ✓ · workers
  polling ✓ — each ✗ comes with its one-line fix (from SKILL.md's preflight list).
- **Re-run with tweaks**: pressing `n` while a run is selected on the Dashboard opens this
  form **prefilled from that run's input** — the fastest way to iterate (same review on a
  new PR, same issue with codex instead, bigger budget after a budget-cap failure).
- Start → jumps straight into Run Detail for the new run.

## 3. Run Detail — watching agents work

```
┌ issue_to_pr 9f2c41 · acme/app #51 · RUNNING 03:12 · 61.2k tok · $0.19 ───────────────────┐
│ Tasks                                    │ fork: implement-api (coding_agent · claude)    │
│  ✓ issue   issue_fetch                   │ turn 7 · 38.1k tok · 2:41 · 3 files            │
│  ✓ clone   git_clone                     │ ──────────────────────────────────────────    │
│  ▾ cp      code_parallel     ▶ RUNNING   │  4  Write src/api/notes.ts                     │
│     ✓ plan     coding_agent  2 subtasks  │  5  $ npm test -- notes                        │
│     ▾ fan_out                            │  6  Edit src/api/notes.ts                      │
│       ▶ implement-api   ▂▄▆ 7 turns      │  7  $ npm test -- notes        ✓ passing       │
│       ▶ implement-ui    ▂▄  5 turns      │  …live turn/command trace, denials in red…     │
│     ○ merge    merge_worktrees           │                                                │
│  ○ push    git_push                      │                                                │
│  ○ pr      pr_create                     │                                                │
└ [t]erminate  [l]ogs  [o]pen URL  [c]onductor UI  [y]ank id  [←→] select task  [esc] ─────┘
```

- **Task tree (left)**: execution order, live statuses. Sub-workflows expand in place —
  `code_parallel` shows its planner and **every parallel fork with its own live turn/token
  count**; watching 5 forks code simultaneously is the signature moment of this screen.
- **Agent panel (right)**: the selected (default: busiest running) `coding_agent`'s live
  trace — `turn N · command` lines streaming (Write/Edit/`$ cmd`), token meter, elapsed,
  backend/model, permission denials highlighted in red. Auto-follows; scroll to pause.
- **On completion the right panel becomes the result card**:

```
│ ✓ COMPLETED in 06:41 · 214k tok · $0.58            │
│                                                     │
│   PR #52  Fix login redirect                        │
│   https://github.com/acme/app/pull/52               │
│                                                     │
│   branch harness/issue-51 · 2 subtasks · 0 conflicts│
│   tokens: plan 6k · code 195k · merge 13k           │
│                                                     │
│   [o] open PR   [n] run again   [esc] dashboard     │
```

  Per-workflow cards: PR URL (issue_to_pr) · review URL + verdict + inline count
  (pr_review) · pushed/reply URL (address_pr) · branch/merged/conflicts (code_parallel).
- **Failure UX**: failed task highlighted red with `reasonForIncompletion` inline; `l`
  opens the logs overlay (the workers write rich structured logs incl. stderr tails);
  footer offers `[r]etry from failed`. A run RUNNING with no task progress for >60s shows
  the amber "no worker polling?" hint (linked to the health strip).

## HITL — responding to prompts

**Shipped (global Approval Inbox).** The `pr_review`, `address_pr`, and `issue_to_pr`
publication gates appear in one global inbox. Press **`a`** from any screen to inspect pending
signal-based WAIT tasks, including gates owned by nested executions. Timed WAITs are excluded;
legacy HUMAN tasks are shown with a registration warning and are not sent through WAIT
signaling. A five-second app-wide poll updates the header badge, removes resolved tasks, and
deduplicates startup/native notifications.

The phase-aware modal offers **Approve**, **Revise with feedback**, **Stop**, and **Later**, with
editable review/PR text. It signals the execution that owns the WAIT task, not necessarily the
displayed parent. For `pr_review` and `address_pr`, Revise and Stop use `COMPLETED` plus a decision
payload so the workflow can start a same-worktree follow-up or record suppression before any
side effect. In LLM mode, bounded automatic revisions run first; an exhausted budget enters the
same inbox. See `../tui/README.md` and `CODING_AGENT_WORKER.md` §"Approval and revision gate".

Generic support for any Conductor WAIT/HUMAN gate (signaled with a status + structured
output that downstream tasks read). Harness workflows adopt a small convention so prompts
render as real questions, with a JSON fallback for anything else:

```json
{ "prompt": "Approve this plan?", "context": {...}, "options": ["approve","reject"],
  "fields": [{"name":"note","label":"Note","type":"text","required":false}] }
```

**Surfacing**: a waiting run shows ⏳ NEEDS YOU on the Dashboard (floats to top), the
header badge increments everywhere, and opening the run auto-opens the respond overlay.
`a` from anywhere jumps to the oldest waiting prompt.

```
┌ ⏳ issue_to_pr 9f2c41 needs your input ── waiting 2m ─────────────────┐
│ Approve this plan before parallel coding begins?                      │
│                                                                       │
│ The planner proposes 3 sub-tasks on acme/app #51:                     │
│   1. implement-api   — REST endpoints for notes (src/api/…)           │
│   2. implement-ui    — list/edit components (src/components/…)        │
│   3. tests           — API + component tests                          │
│ est. budget ≤ $6.00 · backends: claude                                │
│                                                                       │
│ Note (optional)  [_____________________________________]              │
│                                                                       │
│   [ Approve ▶ ]   [ Reject ✗ ]   [ view full context ]   [ esc later ]│
└───────────────────────────────────────────────────────────────────────┘
```

- **The consequence is stated** ("before parallel coding begins"), the **context is
  rendered** (the actual subtasks), and the response is one keypress. Reject completes the
  gate with `approved:false` — the workflow decides what that means (skip / stop).
- **Escape = defer**, never answers. Durable-by-design: the run waits indefinitely; the
  TUI can be closed and reopened days later with the prompt still there.
- Non-conforming HUMAN/WAIT tasks (no convention payload): show the task's input as
  pretty JSON + a status picker (COMPLETED/FAILED) + a JSON output editor — power-user
  fallback so *any* workflow is operable.
- **Publication gate points:** `issue_to_pr` holds before push/PR creation, `pr_review` holds
  before posting, and `address_pr` holds before pushing. `pr_review` and `address_pr` can run
  bounded LLM-judged producer revisions before they require human attention.

## 4. Terminate · retry · re-run

- **Terminate** (`t`, RUNNING runs): confirm modal — *"Terminate pr_review 0b22b6 (acme/app
  PR#7)? Agents stop; the PR/branch keeps whatever was already pushed. [reason optional]"*.
  Cascades to sub-workflows (Conductor semantics) — the modal says so when forks are live.
- **Retry** (`r`, FAILED runs): retries from the failed task, preserving completed work
  (e.g. re-push after a network failure without re-coding). Shown only when applicable.
- **Re-run** (`n` on any run): new run, form prefilled from the old input (see launcher).
- Not offered in v1: pause/resume, restart-from-scratch (available in the Conductor UI via
  `c` if ever needed).

## Keyboard map (global)

| Key | Action | | Key | Action |
|---|---|---|---|---|
| `n` | new run (prefilled if a run is selected) | | `t` | terminate (confirm) |
| `enter` | open selected run | | `r` | retry failed |
| `f`/`F` | cycle/open filters | | `l` | logs overlay |
| `/` | free-text search | | `o` | open PR/review URL |
| `esc` | back | | `c` | open in Conductor web UI |
| `q` | quit (from dashboard) | | `y` | copy workflow id |
| `?` | help / key map | | | |

(v2 adds `a` — jump to oldest NEEDS-YOU prompt.)

## Notifications (v1)

When a run reaches a terminal state while the TUI is open, ring the **terminal bell** and
post an **OS notification** (macOS `osascript` / Linux `notify-send`, silently skipped if
unavailable): *"pr_review acme/app PR#7 — COMPLETED · $0.05"* / *"… FAILED"*. Fires only
for runs started or opened in this TUI session (not the whole fleet), so a busy shared
server doesn't spam. A `--no-notify` flag turns both off. (v2 extends this to NEEDS-YOU
prompts.)

## States & edge UX

| State | UX |
|---|---|
| Server unreachable | Full-screen card: server URL tried, "is Conductor running?", retry countdown. No dead widgets. |
| Workers not polling | Red banner + launcher preflight ✗ ("runs will hang — start workers: `python main.py`"). |
| Run RUNNING, no task progress >60s | Amber row hint "no worker polling?" linking to the health strip. |
| Task FAILED | Red node, reason inline, logs one key away, retry offered. |
| Budget/turn cap hit | Result card names the cap that fired and the knob to raise, `n` prefills a bumped re-run. |
| Empty dashboard | First-run welcome: 3-line quick start + `[n] start your first run`. |
| Terminal too small | Detail collapses to a single column (tree above, panel below); min 80×24. |

## Visual language

- Status: ▶ blue RUNNING · ✓ green COMPLETED · ✗ red FAILED · ◼ grey TERMINATED ·
  ○ dim pending (· ⏳ yellow NEEDS YOU reserved for v2).
- Tokens compact (`54.8k`, `1.2M`); cost always `$x.xx`; both live-ticking while running.
- Agent activity sparkline (`▂▄▆`) on running coding_agent nodes — motion = alive.
- One dark theme in v1.

## Out of scope (v1)

HITL prompts + workflow gates (v2 — designed above), Slack/webhook notifications, browser
UI, multi-server profiles, cost analytics/charts, log follow-mode, editing workflow
definitions, pause/resume, auth management (the TUI inherits `CONDUCTOR_SERVER_URL` +
local `gh` exactly like the CLI does).

## Decisions (reviewed)

1. **HITL → v2.** v1 ships without the attention surface or workflow gates; the design
   above is the agreed direction for v2.
2. **Notifications → v1.** Terminal bell + OS notification on terminal states (see
   §Notifications).
3. **History depth**: 50 most recent runs — sufficient; no paging in v1.
4. **`github_demo` hidden** from the launcher (still on the Dashboard, still CLI-runnable).
