# Specs in the swarm: TLA+, OpenSpec, or none

Status: built (2026-11-14). TLA+ piloted on the user-management app, the Redis-style cache, the Postgres-style
database and three link runs; OpenSpec and the spec choice added the same day.

## Choosing a spec

A spec is optional. Two run inputs decide what the architect writes, each the person's choice or, by
default, the architect's:

| Input | Values | Meaning |
|---|---|---|
| `specs` | `auto` (default) | The architect decides per request: `none` for a small fix, a configuration or documentation change; `openspec` for a feature best stated as requirements and scenarios; `tla` for a stateful core whose rules must hold after any sequence of operations |
| | `tla` / `openspec` / `none` | The person decides |
| `design` | `auto` (default) | The architect writes design documents in `design/` when the change needs them (a new program or part, a changed interface, data or layout) and none for a small fix |
| | `always` / `none` | The person decides |

The architect returns its choice as `spec: {kind, why}` and `design: {docs, why}`; `swarm_formal`, the
gate after planning, holds the plan to the run's choices and proves the spec. `.cc/design.md`, the team's
brief with the contract the checks use, is always written.

**Where things live.** The spec is the coding factory's working material, so it lives in `.cc/`:
`.cc/spec/` for TLA+, `.cc/openspec/specs/<capability>/spec.md` for OpenSpec, next to the checks
(`.cc/checks.json`, `.cc/checks/`) and the adapter (`.cc/conformance/`). Design documents are for the
project's people and stay in `design/`. A pull request leaves `.cc/` and `.conductor-code/` out
(`publishHarnessFiles: true` keeps them); the full delivered commit stays in the checkout at
`refs/conductor/<run>/harness`, and a CI follow-up run restores its harness files from there.

**Later runs.** A spec the repository already has is the starting point: the architect revises it for the
request (or leaves it as it is), never replaces or drops it, and the program is checked against it every
round. The repository's acceptance checks stay too: the plan lists only new or changed checks and the
ones it retires. A TLA+ spec in the old place (`design/spec/`) is moved to `.cc/spec/` at setup.

**A spec cannot vanish.** Measurement checks the kind of spec the plan settled, not whatever the tree
holds: a spec removed after planning is a red check.

## OpenSpec: requirements, each scenario proven by a check

The architect writes requirements in the OpenSpec format (`## Purpose`, `## Requirements`,
`### Requirement:` saying SHALL or MUST, `#### Scenario:` with `- **WHEN**` / `- **THEN**`) and names, on
each acceptance check, the scenarios it proves (`covers`). The harness checks this mechanically
(`common/openspec_spec.py`; the `openspec` CLI is not needed):
- the files must be well formed;
- every scenario must be covered by at least one check, and every covered name must be a real scenario;
- the checks are proven red before the work and run on every committed tree, so a scenario holds exactly
  when its checks pass.

Measurement adds `spec-coverage`, which re-validates the spec and its coverage and lists, per
requirement, whether it holds on this tree. OpenSpec gives traceability from requirement to check, not a
proof beyond the checks; for behaviour that must hold after any sequence of operations, use TLA+.

## What the TLA+ pilots showed

- Five delivered runs (2026-11-14). Every spec passed TLC on the first attempt, in 1–2 s, with 6 to 176
  states and 18 to 2,006 transitions. Every red conformance result was "the program cannot start", which
  the acceptance checks also report. In two of the runs (a configuration file, installer docs) there was
  no behaviour worth modelling, which is why the spec is now a choice.
- Seeded bugs in the delivered user-management app (run dc9624ec, 495 traces):

  | Bug | Conformance | Acceptance checks |
  |---|---|---|
  | delete keeps the user | 494/495 traces diverge | both red |
  | duplicate id accepted | 184 diverge; shortest: `Create(u1) → Create(u1)` expected conflict, got created | API check red |
  | search ignores the query | 134 diverge; shortest: `Create(u2,Ada) → Create(u1,Bob) → Search(Ada)` expected `[u2]` | UI check red |

  Conformance caught every bug, with the shortest action sequence that shows it. The checks caught each one
  too, but each bug only by the check that happened to drive that path. The spec's value there is exhaustive
  coverage of the bounded model and precise diagnosis, not catching what nothing else would.


## Why

The swarm turns a request into code, but what the code is *supposed* to do lives only in prose
(`design/`), in the acceptance checks, and in the code itself. Over time that context decays: a later
change cannot tell which behaviours were intended, and "the checks pass" only says the sampled
scenarios work.

The goal: **a mathematical specification of the program comes first, the code is derived from it, and the
harness checks — mechanically, not by an agent's opinion — that the code implements the spec.** The spec
is the durable source of truth; every later change starts as a change to it.

Constraint: the code stays in ordinary languages (Java, TypeScript, Python, Go, Rust). We do not move
implementation into a verification language.

## The idea: refinement

A program **refines** a spec when every behaviour of the program, seen through an *abstraction function*,
is a behaviour the spec allows. Three parts:

1. **Spec** — a state machine in TLA+ (in `.cc/spec/`): the abstract state (e.g. `users: Id -> User`), the actions
   (`Create`, `Delete`, `Search`, …) with their effects and their observable results (`created`,
   `conflict`, `notfound`, …), and the invariants that must always hold (ids unique, a deleted user is never
   returned, …).
2. **Abstraction function** — maps the program's real state onto the spec's state. For a service it can be
   computed through the program's public interface (list the users over the API and shape them as the
   spec's `users` map), so it needs no change to the program's internals.
3. **Refinement check** — after every action, the program's abstract state equals the spec's next state,
   and the program's observable result equals the spec's.

A full theorem that arbitrary code refines a spec is not practical for mainstream languages. What is
practical, mechanical and rigorous within stated bounds is the following.

## Three layers of evidence

### 1. The design is proven (model checking)

The TLA+ model checker (TLC) explores **every reachable state** of a bounded instance of the spec (e.g. 2–3
ids, 2 names, a bounded number of contacts) and checks every invariant in every state. A violation comes
back as a minimal counterexample trace. This is a proof of the design for that instance, before any code
exists. Spec-quality guard: each invariant must be *able* to fail — the architect states, for each one, a
plausible faulty action that violates it, and the harness model-checks that mutant and requires TLC to
report the violation (the same red-proof discipline the acceptance checks already follow).

### 2. The code is checked against every behaviour of the bounded model (exhaustive conformance)

TLC dumps the full reachable **state graph** of the bounded model, every edge labelled with its action and
arguments. The harness turns it into a **transition-covering trace suite**: for every edge (state *s*,
action *a*, state *s'*), the shortest path from an initial state to *s*, then *a*. Every transition of the
bounded model is exercised — not sampled — and every trace is minimal, so a failure is the shortest
explanation there is.

A generic **conformance runner** in the harness replays each trace against the real program through the
project's **adapter**, and after every step compares the program's abstract state and result with the
spec's. A mismatch is reported as: *from this state, after these steps, action X expected result R and
state S; the program gave R′ and S′* — precise, minimal feedback for the agent that must fix it.

This rests on the small-scope hypothesis: most defects show up in small instances. The bounds are part of
the spec and are stated in the delivery.

### 3. Real executions are checked (trace validation) — phase 2

The program logs each action and its abstract state change in the spec's terms; the harness checks those
logs against the spec, covering executions beyond the model's bounds (AWS does this with P / PObserve,
MongoDB with TLA+). Not in the pilots.

Where a language has one, a symbolic checker can also prove individual functions for *all* inputs
(CrossHair for Python, Kani for Rust, JBMC for Java) — optional, per function, later.

## What is proven, checked and assumed

| | Status |
|---|---|
| The spec's invariants, for the bounded instance | **proven** (TLC, every reachable state) |
| The code matches the spec on every transition of the bounded model | **exhaustively checked** (conformance) |
| Each invariant can fail | **checked** (mutant must be caught) |
| Behaviour beyond the bounds | **validated** in phase 2 (trace validation), assumed in the pilots |
| The spec says what the user meant | **reviewed** — the architect's spec and its invariants are readable in `.cc/spec/` |
| The adapter maps the program correctly | **reviewed**, and kept small (it only calls the public interface) |

## How it sits in the swarm

| Role | Responsibility |
|---|---|
| Architect (plan) | Writes `.cc/spec/<Name>.tla` + `<Name>.cfg` (bounds, invariants) and `.cc/spec/abstraction.json` (the faulty `Next` per invariant, and the mapping of each spec action and variable onto the program's interface); an author session writes the adapter `.cc/conformance/adapter.mjs` from it. |
| Harness: `swarm_formal` (after plan) | Model-checks the spec (invariants must hold), checks every mutant is caught, dumps the state graph, generates `.cc/conformance/traces.json`. A violated invariant, an uncaught mutant or an unparsable spec goes back to the architect with TLC's counterexample (bounded re-plan loop). |
| Implementers | Build the code to the spec and the design. |
| Harness: `swarm_measure` | Runs the acceptance checks **and** the conformance suite (as the check `spec-conformance`); a mismatch is reported as a minimal trace. |
| Round review | Judges failures; fixes code (or, if the spec is wrong, revises the spec — which re-runs `swarm_formal`). |
| Delivery gate | Invariants proven, all mutants caught, every bounded trace conforms, every acceptance check green. |
| Later changes | Edit the spec → model check → regenerate traces → the swarm brings the code along; anything that no longer conforms is caught at once. |

### The adapter protocol

A small long-running program the architect writes against the design's interface (not the code's
internals), speaking JSON lines on stdin/stdout:

```
{"op": "reset"}                                  -> {"ok": true}
{"op": "step", "action": "Create", "args": {...}} -> {"ok": true, "result": "created"}
{"op": "state"}                                   -> {"ok": true, "state": {"users": {...}}}
```

It starts the program itself (the same way the acceptance checks do), and `state` is the abstraction
function. The conformance runner normalises both sides (sets as sorted lists, records as objects, TLA+
strings as strings) before comparing.

### Bounds and size

The architect chooses bounds that keep the reachable state graph small enough to replay quickly (target:
under a few thousand transitions; the harness reports the count and refuses a model that is too large
with a counterexample-free message asking for smaller bounds). Every trace is replayed from `reset`.

## Tools

- **TLA+ / TLC** (`tla2tools.jar`, the official release; Java is already on the host). TLC's state-graph
  dump (`-dump dot,actionlabels`) gives every state and labelled edge; the harness parses TLA+ values
  (records, functions, sets, sequences, strings, numbers, booleans) into JSON. Installed once per host in
  the harness tool cache, like the shared browser.
- **Quint** (TLA+ semantics, programming-language syntax) is a later option for the spec language if
  architects write it more reliably; it compiles to TLA+, so the rest of the pipeline is unchanged.

## Plan

1. **Tooling** — `common/formal_tools.py`: install/locate `tla2tools.jar` in the harness cache; run TLC with
   a config; parse results (invariant violation + trace, state counts).
2. **State graph → traces** — `common/formal.py`: parse the dot dump and TLA+ values; build the
   transition-covering suite (BFS shortest path to every edge's source, then the edge); write
   `.cc/conformance/traces.json` with the bounds and counts.
3. **Mutants** — run TLC on the spec with each mutant action substituted; each must violate its invariant.
4. **Conformance runner** — `common/conformance.py`: drive the adapter, compare per step, report the first
   mismatch of each failing trace (deduplicated by action), bounded output.
5. **Swarm wiring** — `swarm_formal` worker + a bounded plan ↔ formal loop in `swarm.json`; `swarm_measure`
   runs conformance when a spec exists; prompts for the architect (spec, abstraction, mutants, adapter) and
   for the round review (conformance failures).
6. **Console** — the run's result shows the spec kind and its proof, and the design documents; the spec's
   checks appear with the other checks, and `.cc/spec/` is browsable in the Files tab.
7. **Tests** — a tiny spec with a known bug (TLC finds it), a spec with an uncaught mutant (rejected), a
   trace suite against a fake adapter that diverges on one transition (reported minimally).
8. **Pilot 1: user-management app** — the swarm builds it from the request with a spec; report: invariants,
   bounded state/transition counts, conformance results, time and cost against run 65e43971.
9. **Pilot 2: the Redis-style cache** — concurrency-free command semantics first (SET/GET/DEL/EXPIRE with
   a logical clock, eviction), same report.
10. **Phase 2 (after the pilots)** — trace validation from program logs; symbolic per-function proofs where
    the language has a checker; OpenSpec requirements linked to invariants.
