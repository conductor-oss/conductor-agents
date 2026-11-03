# Swarm: finding the toughest hidden vulnerabilities by search + synthesis

> **Thesis.** The deepest, most unobvious vulnerabilities are *chains and interactions that no
> single agent ever observes end to end*. You find them with two things working together:
> **massive parallelism** (to search a huge hypothesis space — many framings, each pushed deep)
> and a **collaboration loop** (to synthesize the independent partial findings into emergent
> chains, round over round). Everything else in this document — the execution tree, the shared
> board, the oracle tier, the safety ceilings — exists only to let that loop run *hundreds of
> agents wide, safely, with trustworthy output.* They are load-bearing, but they are not the point.

This is the design for scaling the harness from "one expert pentester that doubts itself" (today:
~8 exploit agents per pass, passes serial) to a **team of hundreds** that works like a human red
team: independent work, a standup to share and critique, follow-the-nose reprioritization, and a
kill chain that compounds.

---

## 1. Why the hard bugs need this

A single agent — however capable — fails at the toughest bugs for three structural reasons:

1. **It commits to a few obvious framings.** Hidden bugs are hidden because the correct entry
   framing is improbable. One agent explores a handful; the improbable-but-correct one is never
   tried.
2. **It abandons leads at the first blocker to save budget.** Tough bugs sit *behind* blockers
   (a filtered payload, a 200 that looks benign, an egress denylist). Depth is expensive; a lone
   agent can only afford to push one or two threads.
3. **It never holds two unrelated partial observations at once.** The highest-impact bugs are
   *compositions*: an SSRF primitive found while probing one feature + an internal service name
   glimpsed in an unrelated response = internal RCE. If one mind never holds both, the composition
   never happens.

Parallelism and the collaboration loop attack exactly these three, and **only together**:

| Failure of the lone agent | What fixes it |
|---|---|
| Few framings tried | **Parallelism** — hundreds of *diverse* agents search many framings at once |
| Leads abandoned at blockers | **Parallelism** — afford to push dozens of threads deep simultaneously |
| No composition of partial findings | **Collaboration loop** — the standup synthesizes the board into chains |
| Many tries → many false alarms | **Verification tier** — only oracle-backed, refuter-survived findings promote |

---

## 2. The core loop (the spine)

The campaign is **round-based**, like a team working in sprints with a standup between each.

```
ROUND:
  1. STANDUP (lead/allocator)
       read the board for this campaign → coverage map, hot leads,
       verified-primitive pool, per-area credit, remaining budget
       → produce a WAVE PLAN: a set of assignments sized to budget
  2. WAVE (massive parallelism — the hundreds live here)
       FORK_JOIN_DYNAMIC spawns the assignments as independent sub-workflows.
       Each agent works ALONE and only WRITES to the board. No agent reads
       another mid-flight — so there is zero contention to coordinate.
  3. JOIN + SYNTHESIZE (the hidden-bug engine — see §4)
       single-writer reconcile: dedup, update coverage, promote verified
       findings, retire dead ends, and COMPOSE partial observations into
       new chain-hypotheses + spawn chainers on verified primitives.
  4. REFLECT
       continue while (untested high-value area OR fresh primitive) AND budget remains;
       else stop. Hard cap on rounds.
```

**Independent work inside the wave, all sharing at the join.** This "branch-shallow, join-often"
shape is what lets the whole thing coordinate through durable storage and the tree alone — no
live lock, no shared mutable hot state, no Redis required to start (see §6).

---

## 3. The fuel: massive parallelism

The wave is where hundreds of agents run. Three things make the parallelism *productive* rather
than merely loud:

- **Diversity by construction.** Scouts are assigned distinct `(model, persona, temperature,
  surface-region, objective-bias)` tuples. They are an **ensemble**, not clones — that is what
  covers the improbable framings. N identical agents find the same obvious bug N times; N diverse
  agents cover N regions of the hypothesis space.
- **Depth held in parallel.** Each promising lead is a ladder walk (the existing `exploit_deepen`
  engine) that refuses to stop at the first block. Parallelism means dozens of these run at once,
  so breadth-over-framings and depth-over-blockers happen *simultaneously*, which a small agent set
  cannot do.
- **Recursive fan-out.** A verified primitive spawns a fresh sub-tree of chainers. A kill chain is
  therefore a *nested sub-workflow tree*, and the swarm grows toward wherever the signal is.

Wave width is a **budget decision**, not a constant. The allocator sizes each wave to credits
under a hard ceiling (§6). "Hundreds, thousands if budget allows" is literally the width knob —
but it is only turned up while the measured confirmed-findings-per-dollar curve is still climbing
(§7).

---

## 4. Synthesis at the standup — the hidden-bug engine

**This is the most important section.** Massive parallelism produces discovery; the standup
produces *the deep bugs*, by composing what independent agents each saw only part of.

The board accumulates, per campaign, every agent's partial output: hypotheses, observations,
attempts-with-lessons, **verified primitives**, findings, territory claims, and critiques. At each
join the allocator does what a human lead does at standup:

- **Compose primitives into chains.** SSRF primitive (agent A) + internal service marker
  (agent B) → spawn a chainer that targets internal RCE. Credential-held (agent C) + cross-tenant
  object ref (agent D) → spawn a chainer for cross-tenant takeover. This replaces the fixed
  `chaining.py` unlocks map with **emergent** chaining off whatever is on the board.
- **Compound across rounds.** Every verified primitive *raises the floor for the whole swarm*. A
  4-deep chain is unreachable in one agent's context but reachable when rounds 1–3 established the
  preconditions and round 4 stands on them. Knowledge grows monotonically on the board.
- **Give feedback.** The lead turns the board into a per-node `focus_directive` for the next wave
  ("this control blocks X, pivot to Y"; "primitive Z is live, chain it"). Teammates critiquing
  each other's leads, mechanized.
- **Peer review.** Every candidate finding spawns a **refuter** (different model/identity) that
  tries to break it. It survives only if the refuter fails *and* the oracle fires.

> The collaboration loop is a synthesis engine: it turns N independent partial views into emergent
> chains that no single view contained. That compositional step is where the toughest, most hidden
> vulnerabilities actually come from.

---

## 5. The roles (division of labor)

Each is a Conductor workflow type; a node in the tree.

| Role | Job | Status |
|---|---|---|
| **Lead / allocator** | Run the standup: read board, plan the wave, size by credit | build |
| **Scouts** | Breadth: diverse, cheap; enumerate, probe lightly, post hypotheses, claim territory | build on `explore` |
| **Exploiters** | Depth: the `exploit_deepen` ladder walk, one per claimed hypothesis | have |
| **Chainers** | Consume **verified** primitives from the pool; pursue deeper multi-step hypotheses; recursive | build; `chaining.py` reads the pool |
| **Refuters** | Adversarial peer review: try to break every candidate finding | elevate existing verify |
| **Verifiers** | Deterministic oracle re-issue; the *only* promoters of a finding | have (extend oracles) |
| **Scribe** | Roll the board up into interim + final dossier by campaign id | build |

Only **oracle-verified** primitives are chainable, so a false "confirmed" can never poison the
chainers.

---

## 6. What makes it possible at scale (subordinate systems)

These are load-bearing but not the point. Each maps a swarm need to the cheapest mechanism.

**Structure & lifecycle — the execution tree (mostly native Conductor):**
- Campaign root `workflowId` is stamped as `correlationId` down every `subWorkflowParam` → the
  whole swarm is one query.
- Every agent is a sub-workflow; `parentWorkflowId` is the edge; nesting is swarm depth.
- **One kill switch: terminate the root, which cascades.** Window expiry, kill-file, budget breach,
  and policy breach all collapse to "terminate root" — instant, not next-pass-boundary.

**Shared memory — the board (build):**
- **S3 append-log** keyed by `correlationId`, one object per fact named by author `workflowId`.
  No shared key → no lost updates → no lock. (Retires the `budget.py` flock file, which will not
  survive hundreds of writers.)
- **S3 artifact store** via Conductor external payload storage for blobs (evidence, transcripts,
  PoCs); only references ride the workflow, keeping big payloads out of task I/O.
- **Read model**: assemble campaign state (coverage, primitive pool, global tried-set, findings)
  by `correlationId`. Used by the allocator and scribe.
- Live mid-flight cross-branch reads (Redis) are an **optional upgrade**, justified only if §7
  shows mid-wave chaining pays. Not a prerequisite — the join-point synthesis covers it.

**Trust — the filter that makes breadth worth anything (the hard prerequisite):**
- Only a verifier worker that **re-issues the oracle** may promote a finding.
- **Oracle breadth** is the gate: today SSRF-differential + arithmetic-RCE; add time-blind SQLi,
  stored-XSS-in-victim-context, race-condition invariant, path-traversal canary, auth-state
  illegal-transition. *Without this, N agents produce N piles of unconfirmed candidates.*
- Global tried-signature set (consulted at standup) + cross-run fingerprint memory → waves never
  repeat each other or prior runs.

**Safety at scale (offensive against a live target):**
- **Global throughput ceiling** via task-def `concurrentExecLimit` + rate limit on `http_request`
  and `code_exec` — caps real load on the target no matter how wide the tree grows.
- **Fix the redirect/scope-cross + credential-forward hole in `http_request` first.** One agent
  following a redirect out of scope is a finding; hundreds forwarding `X-Authorization` is an
  incident. This blocks fan-out.
- Every node inherits `scope`, `capability_max`, and the manifest; the per-request gates run on
  every call.
- Halt governor reads the board and terminates the root on policy/window/budget/sensitive-bulk
  breach.
- Tamper-evident audit log aggregated to S3 by `correlationId`.
- **Untrusted-content discipline**: target-derived content and board facts are **data, never
  instructions** — a swarm that communicates through content is a prompt-injection surface (it is
  how the real-world reference swarms coordinated).

---

## 7. How we know it's working (don't scale blind)

- **Measure on `bench/`**: confirmed findings per dollar vs. wave width. Turn width up only while
  that curve climbs.
- **Observability rollup**: coverage, confirmed, spend by `correlationId` — at thousands of nodes
  you need the aggregate, not the per-node tree.
- **Reproducibility**: each node's inputs/outputs are durable by `workflowId` (Conductor + S3), so
  every finding replays and every run is auditable.

---

## 8. Critical path (build order)

Strictly ordered — each unblocks the next.

0. **Prerequisites that gate everything:** oracle breadth (§6 trust); fix the redirect/scope hole;
   task-def concurrency + rate ceilings. *A wide swarm with 2 oracles = a wide pile of false
   positives and a bigger blast radius.*
1. **Board:** S3 append-log + artifact store; `correlationId` stamped down the tree; scribe rollup.
2. **The loop:** replace the fixed `max_hypos` fork in `assess_pass.json` (`build_exploit_jobs`)
   with an allocator-sized wave + a reconcile/synthesize step at the join. **This is the spine —
   it is what turns parallelism into a team.**
3. **Roles + credit:** scouts, chainers (pool-driven), refuters; the credit allocator; the lead.
4. **Prove + scale:** measure on `bench/`, then open the wave width while the curve climbs. Add
   Redis for live sharing only if the data says mid-wave chaining pays.

---

### One-line summary

> Hidden vulnerabilities are a **search-and-synthesis** problem. **Massive parallelism** does the
> search — many diverse framings, each pushed deep, held at once. The **collaboration loop** does
> the synthesis — composing independent partial findings into emergent chains that compound round
> over round — and the **verification tier** keeps that breadth trustworthy. The tree, the board,
> and the ceilings are what let it run hundreds wide, safely. Build the loop and the oracles first;
> the rest is plumbing.
