# Closure result — every roadmap hypothesis earned (2026-11-06)

Executes [`SDLC_CLOSURE_PLAN.md`](SDLC_CLOSURE_PLAN.md) WP1–WP10. Every claim below cites a real Conductor
execution of the harness's own workflows (local OSS server, blackboard on Postgres from WP4 on). Fixture
repos are small Python projects built for each proof; the harness code that ran is the code in this tree.
Where a proof first failed, the failure, its mechanism and the fix are listed — the passing run is the one
after the fix. Suite: `make test` 1690+ passed; `scripts/audit_harness_invariants.py` clean; `runtime_health`
healthy.

> **Design change, 2026-11-07: humans are passive observers.** The swarm no longer waits for any
> human decision: the `human_decision` WAIT gate and the `humanAvailable` input are gone from
> `team_build`, `cc_program` and `sdlc_intake`. A frozen-contract edit is rejected by the Lead (the
> H4 approve path was removed); an honored pushback exempts that one item from that one directive
> (H16); a quarantine or starvation ends the run naming its mechanism (H2). Humans watch, steer in
> plain language, stop agents and terminate runs. The H2/H4/H16 rows below cite the runs that proved
> the earlier gate behaviour; the current behaviour is pinned by the suite and the AGENTS.md
> Swarm Human-Role Policy. Live finding that prompted it: a gate re-armed on every wave and the TUI
> approval dialog approved it 16 times, spending all 16 waves of a team (run `8f1fa836`).

## Scorecard

| H | Status | Proof (execution id) |
|---|---|---|
| H1 write-concurrency | ✅ | 30 forks on one branch, zero merges (`f88fb76f`); concurrent child teams interleave linearly on one program branch (`45e7039a`, 0 merge commits) |
| H2 convergence | ✅ | review cap quarantines, never accepts (`621140ae`: INCOMPLETE `review_rounds_exhausted`); starved run exits after 1 of 12 waves naming why (`2c6ca1d1`); human gate holds then delivers (`5d940cd6`) |
| H3 hierarchy | ✅ | program → 3 child teams, rank 1 concurrent, program directive reaches a child (`45e7039a`); recursion program → program → 2 concurrent teams (`8c68ff96`); planner-driven 3-rank program (`223d8139`) |
| H4 frozen contracts | ✅ | contract edit held → human approve → revision 2, dependent re-verified (`8ad3d511`); reject → re-dispatched with the constraint, contract untouched (`0ffe7fd1`) |
| H5 hard budget | ✅ | spend by role in every run, incl. reviewer, planner, test author (`c01eb460` team: planner $0.19, test_author $0.25, implementer $0.41, reviewer $0.10); child spend on the program budget, equal shares per rank (`29ba7065`: $19.89/child) |
| H6 substrate | ✅ | Postgres board for every run from round 2; 8-wide (`c6014e53`) and 30-wide (`f88fb76f`, standup 83 ms) on it; incremental fold = one parse per fact (unit-proven, 1,000-item board) |
| H7 rollup/exceptions | ✅ | exception queue + console drove every human decision below |
| H8 review not theater | ✅ | `unavailable` review never passes (`31b5e859`: blocked `reviewer_unavailable`); reviewers see directives (fix after `b9446263`, `dcc695a6`) |
| H9 zero ceremony | ✅ | single piece → `code_parallel`, no boards (Phase 2 `11d5e604`) |
| H10 lesson decay | ✅ | `path::symbol` decay in a real `repo_memory_read`: file exists, symbol gone → invalidated 1, live 1 (`70b1ad2e`) |
| H11 feedback latency | ✅ | advisory nudge injected into the running session 1.79 s after posting, `total()` delivered the same wave; blocking stop interrupted within 1.6 s (`e0b8483d`) |
| H12 three modes + best-of-N | ✅ | disjoint request → `parallel` (`848dd93f`); shared-file request → `team` (`0c9e2eb2`); best-of-2 per item, winner by rule, loser discarded (`85339559`) |
| H13 gameable oracle | ✅ | acceptance tests authored by a different agent, red at base, frozen, sandboxed red run, tamper guard clean, DELIVERED (`84afb824`) |
| H14 decomposition gate | ✅ | bad cut rejected before any agent spawned, re-planned with reasons, round 2 routed `team` and delivered (`c01eb460`) |
| H15 shared error | ✅ | reviewer retracted a wrong lesson swarm-wide (`65c30950`); implementer lessons capped as opinion |
| H16 constraint lifecycle | ✅ | unauthenticated steer → 401; authored as `human:viren@orkes.io`; impossible constraint → agent pushback → human lift → next dispatch carries 0 directives → DELIVERED (`196142ca`) |
| H17 durability/sandbox | ✅ | worker killed mid-session + worktree wiped → retry restored `stats.py` from checkpoint `08113979` → DELIVERED (`56891527`); checks run in the OS sandbox (`84afb824` final `make test` sandboxed=true); `runtime_health` healthy (`2bfd3d76`) |
| Scale | ✅ to 30 | 30/30 in one wave (`f88fb76f`): 16 concurrent sessions, 14 queued |

Recovery drill: `56891527-9a1d-4fbd-9813-384f75b791c5`.

## Live-found defects fixed during the build

| Run | Mechanism | Fix |
|---|---|---|
| `2c6ca1d1` | targeted test cycle on a branch with nothing integrated reported "no changed paths", mislabelled not-green | team final gate runs `test_cycle` in `full` mode |
| `b9446263` | reviewer, blind to steering, asked to undo a human-directed change | reviewer prompt carries the item's directives, human first |
| `dcc695a6` | child-team reviewer blind to a program-board directive rejected work 3× | reviewer also gets parent directives |
| `87381848` | stop posted in an agent's final tool-free turn was never injected | `Stop` hook re-polls (bounded to 3 re-entries) |
| `f6015e75`, `de79d076` | DO_WHILE condition read a stale variable / unresolved in-loop ref with a SIMPLE body → replan never looped (reproduced with probe workflows) | intake/program loops read the loop's own per-iteration output |
| `25fe604e` | concurrent acceptance authors in one worktree reverted each other's files | per-author worktrees + `team_acceptance_collect` |
| `484c7a00` | implementer used pushback for "could not run tests", blocking a correct delivery | pushback counts only if it names a directive in the item's dispatch record |
| (probe) | Seatbelt `(local ip "localhost:*")` re-opened the internet | loopback allowed only as the remote side |

## External limit (named, with the removal step)

16 concurrent coding sessions per host: `polling.worker_thread_count` caps a task at 16 threads and
`main.py`'s singleton guard refuses a second `coding_agent` deployment on one host. A wider wave queues
(proven: 30 items, 16 at a time). Removal: a second host running `WORKER_MODULES=coding_agent` against
the same server and board adds 16 slots.
