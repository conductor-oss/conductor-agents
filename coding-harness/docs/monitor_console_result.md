# Monitors & Steer-by-Exception — Phase 3 Result

**Status: shipped and live-proven.** The team cycle now runs with a **monitor** on every wave
(bounded authority: autonomous small corrections, escalate the rest), a durable **exception queue**,
a **web Swarm Console** that is a view+editor over the boards, and **push notifications**. Proven by a
real Conductor execution plus live console steering.

## What shipped

- **Monitor role** (`workers/common/monitor.py` + `team_monitor` worker, wired into every `team_build`
  wave). It watches the board (§4.5) and classifies each signal by the open-question-#2 authority
  boundary: **autonomous** (a *stall/thrash* → nudge + re-prioritize; a *quality regression* →
  directive to address the findings) vs **escalate** (a *blocked* item, *duplication*, a *budget
  breach* → the exception queue for a human). It errs toward escalation; it never kills work, expands
  scope, or spends on its own. Its primary output is the human rollup + the open-exception count (H7).
- **Exception queue** (`team_board` `exception` kind + `open_exceptions`/`resolve_exception` fold):
  the durable list of decisions a human must make, foldable by (item, kind), append-only like the rest
  of the board.
- **Web Swarm Console** (`tui/console.py`, `make console`): a stdlib-only server that is a *view +
  editor over the boards* (§4.8) — rollup, **exception queue as the landing surface**, kanban by
  state, **cost-burn vs. ceiling**, learning board, and agent drill-down, with steering POSTs
  (nudge / reopen / reprioritize / resolve) that are the same authored board writes an agent makes.
- **Push notifications** (`console.push_if_needed`, reusing the TUI notifier): a days-long run pings
  the human only when the open-exception count rises — steer by exception, not by watching.

## Live proof — monitor corrects autonomously, then escalates (`120f5c7b-daeb-4b2d-be91-a53d4537b8fb`)

Target: a two-item run where `email` is a real feature and `ghost` is a deliberate **no-op**
(instructed to make no changes), with `maxAttempts=3`, `stallAttempts=2`. The trace is exactly the
designed behavior:

- **Wave 1** — standup dispatched **`email` + `ghost` concurrently** (disjoint files → team size 2);
  `email` integrated to review, `ghost` delivered no commit → re-queued (attempt 1). `monitor__1`: no
  signals (attempt 1 < stall threshold).
- **Wave 2** — `ghost` re-dispatched, empty again → re-queued (attempt 2). **`monitor__2`:
  `signals=['stall']`, `actionsApplied=2`** — the monitor **autonomously** posted a corrective
  directive *and* re-prioritized `ghost` (priority → 102), **with no human involved**.
- **Wave 3** — `ghost` empty at the retry cap → **blocked**. **`monitor__3`: `newExceptions=1`,
  `openExceptions=1`** — the monitor **escalated** the blocked item to the exception queue.
- **Honest outcome:** `INCOMPLETE — 1 item(s) blocked (ghost)`, `openExceptions: 1`, with the
  exception on the board naming the precise mechanism (`fork ghost produced no commit …`).

Verified on the board after the run: one `monitor`-authored directive on `ghost`
(*"You have retried item 'ghost' 2 times without landing it. Change approach…"*), `ghost` priority
`102`, and one open exception.

## Live proof — human steer-by-exception from the console

With the Swarm Console running against the live blackboard:
- `GET /api/board?run=…` returned the open exception queue (the blocked `ghost`) — the default landing
  surface, not the raw firehose.
- `POST /api/steer {action: "resolve", item: "ghost", kind: "blocked"}` → `{"ok": true}`, and the open
  exception count went **1 → 0**. A human drove the decision from the exception queue in the console,
  and the resolution is a durable board fact.

The console was also verified serving a *delivered* run's board (the Phase-2 `2501ed93` run: 3/3 done,
3 lessons, cost-burn $0.52/$20) — rollup, kanban, lessons, and cost-burn all render from the live fold.

## Tests

`workers/tests/test_monitor.py` (detection classification: stall→autonomous, quality→autonomous,
blocked/duplication/budget→escalate; the `team_monitor` worker applies actions, posts de-duplicated
exceptions, and resolution closes them) and `tui/tests/test_console.py` (the pure request logic:
`/api/runs`, `/api/board` with cost-burn, the page, and steering POSTs as board writes; push fires only
when exceptions rise). Full suite green (1581 passed, 3 skipped); invariant audit clean.

## Honest note

The monitor-demo run reported `test_cycle=command_discovery_blocked` as a *secondary* signal. That is
an artifact of the demo fixture — it names a module `email.py`, which shadows Python's stdlib `email`
package when pytest bootstraps from that directory — not a monitor or team-cycle defect. The run's
primary verdict (INCOMPLETE because `ghost` is blocked) is correct and is what the monitor demo set out
to show.

## Recommendation

**Phase 3 complete → proceed to Phase 4 (stage front door & bug depth: `sdlc_intake` G1 + `bug_fix`
G2).** A long run can now be watched and driven by exception: the monitor corrects small drift itself
and escalates real decisions to a human who steers from the console, with push notifications so they
need not watch.
