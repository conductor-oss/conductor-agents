"""What the swarm specifies before it builds, and how that is checked (docs/FORMAL_SPEC_DESIGN.md).

Two choices, each the person's or, by default, the architect's:

- ``specs``: ``auto`` (the architect decides per request), ``tla`` (a TLA+ state machine, model-checked,
  and the program replayed against every transition of it), ``openspec`` (requirements and scenarios,
  every scenario proven by an acceptance check) or ``none``.
- ``design``: ``auto`` (the architect writes design documents when the change needs them and skips them
  for a small fix), ``always`` or ``none``.

The spec is the coding factory's working material and lives in ``.cc/`` (``.cc/spec/`` for TLA+,
``.cc/openspec/`` for OpenSpec). The design documents are written for the people who own the code, and
live in ``design/``. A spec that a repository already has is the starting point of the next run: it is
revised, never silently replaced or dropped, and the program is checked against it every round.
"""
from __future__ import annotations

import json
import os

from common import formal, git, openspec_spec

SPEC_MODES = ("auto", "tla", "openspec", "none")
DESIGN_MODES = ("auto", "always", "none")
KINDS = ("tla", "openspec", "none")
NAMES = {"tla": "TLA+", "openspec": "OpenSpec", "none": "no"}
A = {"tla": "a TLA+", "openspec": "an OpenSpec"}
WHERE = {"tla": formal.SPEC_DIR + "/", "openspec": openspec_spec.SPECS + "/"}
CHECKS_FILE = os.path.join(".cc", "checks.json")


def spec_mode(v) -> str:
    m = str(v or "").strip().lower() or "auto"
    if m not in SPEC_MODES:
        raise ValueError(f"specs must be one of {', '.join(SPEC_MODES)}, not {v!r}")
    return m


def design_mode(v) -> str:
    m = str(v or "").strip().lower() or "auto"
    if m not in DESIGN_MODES:
        raise ValueError(f"design must be one of {', '.join(DESIGN_MODES)}, not {v!r}")
    return m


# ─────────────────────────────── what the repository already has ───────────────────────────────
def migrate_legacy(work: str) -> bool:
    """Runs before 2026-11-14 kept the TLA+ spec in ``design/spec/``; move it to ``.cc/spec/`` (a rename
    the first round commits) so there is one place to look."""
    old, new = os.path.join(work, formal.LEGACY_SPEC_DIR), os.path.join(work, formal.SPEC_DIR)
    if not os.path.isfile(os.path.join(old, "abstraction.json")) or os.path.exists(new):
        return False
    os.makedirs(os.path.dirname(new), exist_ok=True)
    git.git(work, "mv", formal.LEGACY_SPEC_DIR, formal.SPEC_DIR)
    return True


def existing_kind(work: str) -> str:
    if os.path.isfile(os.path.join(work, formal.SPEC_DIR, "abstraction.json")):
        return "tla"
    if openspec_spec.spec_files(work):
        return "openspec"
    return ""


def read_checks(work: str) -> list[dict]:
    try:
        with open(os.path.join(work, CHECKS_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [c for c in data if isinstance(c, dict) and c.get("id") and c.get("command")] if isinstance(data, list) else []


def merge_checks(existing: list[dict], plan: dict) -> list[dict]:
    """The run's checks: the repository's existing ones (they keep proving what earlier runs built), minus
    those the architect retires, with the plan's new or changed ones (by id) on top."""
    retired = {str(x) for x in plan.get("retireChecks") or []}
    planned = [c for c in plan.get("checks") or [] if isinstance(c, dict) and c.get("id") and c.get("command")]
    ids = {str(c["id"]) for c in planned}
    keep = [c for c in existing if str(c["id"]) not in retired and str(c["id"]) not in ids]
    return [{"id": str(c["id"]), "command": str(c["command"]), "covers": list(c.get("covers") or []),
             **({"smoke": True} if c.get("smoke") else {})} for c in keep + planned]


def design_docs(work: str) -> list[str]:
    base = os.path.join(work, "design")
    if not os.path.isdir(base):
        return []
    return sorted(os.path.relpath(os.path.join(d, f), work) for d, _, fs in os.walk(base) for f in fs
                  if f.endswith(".md"))


# ─────────────────────────────── the architect's instructions ───────────────────────────────
TLA_GUIDE = """Write the TLA+ SPEC of the program's SEMANTIC CORE, the mathematical source of truth the code is derived from and proven against, in `.cc/spec/`. Model what is subtle and must stay correct (the state and its rules: identity, types and type errors, expiry against a logical clock, ordering, atomic multi-part operations, what a query must return), with each family of commands as an action over that core. Do not model every command or field: the long tail of simple commands is covered by the acceptance checks.
   - `<Module>.tla`: a TLA+ state machine. VARIABLES: the abstract state (for example `users`, a function from id to a record of the fields that matter) and one variable holding the observable RESULT of the last action (for example `result`, a string such as "created", "conflict", "notfound", or the value a query returns). `Init`, one operator per user-visible action with explicit parameters (`Create(id, name)`, `Delete(id)`, `Search(q)`), each covering its success AND its failure cases and setting the result, and `Next == \\E ...: Action(...) \\/ ...`. INVARIANTS that state what must always hold (ids unique, a deleted item is never returned, a search returns exactly the matching items, ...). Use only `Naturals`, `Sequences`, `FiniteSets`, `TLC`; strings in double quotes; the empty function is `[x \\in {} |-> 0]`; a function update is `[f EXCEPT ![k] = v]` or `[j \\in DOMAIN f \\cup {k} |-> IF j = k THEN v ELSE f[j]]`.
   - `<Module>.cfg`: `CONSTANTS` with SMALL bounded domains (2 or 3 values each, e.g. `Ids = {"u1", "u2"}`), `INIT Init`, `NEXT Next`, one `INVARIANT` line per invariant. A .cfg constant can only be a string, a number or a set of them (no tuples, records or functions): define anything else (a lookup table, a relation) as an operator in the .tla. Abstract away fields that do not change behaviour (give them one or two values); the model checker visits every reachable state and the code is replayed against every transition, so keep the model to at most a few thousand transitions.
   - `abstraction.json`: `{"module": "<Module>", "config": "<Module>.cfg", "state": [the variables compared after every step], "result": "<the result variable>", "adapter": "node .cc/conformance/adapter.mjs", "mutants": [{"invariant": "<Inv>", "next": "<NextWithAFault>"}, ...]}`. For EVERY invariant, define in the .tla a faulty variant of `Next` (one action deliberately wrong) that breaks it; the harness proves each one is caught, so no invariant is vacuous.
   - in `abstraction.json`, also `"mapping"`: how each spec action is performed through the program's public interface (the request or command, its arguments) and how each answer maps to the spec's result value, and how each state variable is read back (the calls, and how their answers become the spec's shape). An adapter author writes `.cc/conformance/adapter.mjs` from it after you, so make it complete and exact.
   Do NOT run TLC or any model checker yourself: the harness model-checks the spec the moment you finish and returns TLC's message (a violated invariant, a fault it did not catch, a model too large, a parse error) for you to fix. Then the code is replayed against every transition of the model after every round."""

OPENSPEC_GUIDE = """Write the OPENSPEC SPEC: the requirements the program must meet, each with the scenarios that show it, in `.cc/openspec/specs/<capability>/spec.md` (one file per capability, a short kebab-case name such as `user-directory`). The format:
   ```
   ## Purpose
   <one or two sentences: what the capability is for>

   ## Requirements
   ### Requirement: <name>
   The system SHALL <the behaviour, in one or two sentences>.

   #### Scenario: <name, unique across the whole spec>
   - **WHEN** <the situation and the action>
   - **THEN** <what is observed>
   - **AND** <more that is observed>
   ```
   Every requirement says SHALL or MUST and has at least one scenario; cover its failure cases (invalid input, a conflict, something not found) as scenarios too. Every scenario must be proven by an acceptance check: name the scenarios each check proves in its `covers` (the exact scenario names). The harness validates the spec and the coverage the moment you finish and returns what to fix; then a scenario holds exactly when its checks pass on the committed tree."""

CHOICE_GUIDE = """Decide whether this request needs a SPEC, and which kind, and return it as `spec` (`kind`: "tla", "openspec" or "none"; `why`: one sentence):
   - "none": a small fix, a configuration, build or documentation change, or anything whose behaviour the acceptance checks state completely. Most small requests.
   - "openspec": a feature whose behaviour is best stated as requirements and scenarios (user-visible flows, an API's resources, validation and error rules).
   - "tla": a stateful core with subtle rules that must hold after ANY sequence of operations (identity and uniqueness, deletion, ordering, expiry against a clock, transactions, concurrency, a protocol's states), where sampled scenarios are not enough.
   Then, for the kind you chose, follow the matching instructions:
   (tla) {tla}
   (openspec) {openspec}"""


def plan_guide(specs: str, design: str, have: str, checks: list[dict], docs: list[str]) -> dict:
    """The plan prompt's ``{{specGuide}}``, ``{{designGuide}}`` and ``{{existingChecks}}``."""
    if specs == "none":
        spec = 'Write NO spec: the person chose none for this run. Return `spec` with `kind` "none".'
        if have:
            spec += (f" The repository's existing {NAMES[have]} spec in `{WHERE[have]}` is not checked on this run; "
                     "leave it untouched.")
    elif have and specs in ("auto", have):
        spec = (f"This repository already has {A[have]} spec in `{WHERE[have]}` from earlier runs: it is the "
                "source of truth for the behaviour it covers. Read it first. Revise it for this request (change what "
                "the request changes, add what it adds); never replace or drop it. When this request does not touch "
                "what it specifies, leave it as it is: the harness still checks the program against it every round. "
                f'Return `spec` with `kind` "{have}" and `why`.\n   '
                + (TLA_GUIDE if have == "tla" else OPENSPEC_GUIDE))
    elif specs in ("tla", "openspec"):
        spec = (f'The person asked for {A[specs]} spec: write it, and return `spec` with `kind` "{specs}" and `why`.'
                + (f" (The repository's {NAMES[have]} spec in `{WHERE[have]}` is not checked on this run; leave it.)"
                   if have and have != specs else "") + "\n   " + (TLA_GUIDE if specs == "tla" else OPENSPEC_GUIDE))
    else:
        spec = CHOICE_GUIDE.format(tla=TLA_GUIDE, openspec=OPENSPEC_GUIDE)
    existing_docs = (" The project's design documents now: " + ", ".join(f"`{d}`" for d in docs[:20]) + "."
                     if docs else " The project has no design documents yet.")
    body = ("Cover the components and directory layout, the interfaces between parts (names, routes, data shapes, "
            "with an example), the important decisions and why, and how the program is started and used. How many "
            "depends on the design: a small program needs one (`design/architecture.md`); a larger one adds a "
            "document per part that deserves its own (for example `design/api.md`, `design/data-model.md`, "
            "`design/ui.md`). If the project already has design documents, change only the ones this request "
            "affects. Keep them concrete; the team builds from them.")
    if design == "none":
        guide = ("Write NO design documents in `design/` (the person chose none for this run): everything the team "
                 "needs goes in `.cc/design.md` below. Return `design` with empty `docs`.")
    elif design == "always":
        guide = ("Record the architecture as DESIGN DOCUMENTS in `design/` at the repository root: they are part of "
                 "what is delivered, written for the people who will own this code as much as for the team building "
                 "it. " + body + " Return `design` with `docs`: every document in `design/` that applies to this "
                 "request (written, changed or already right), and `why`.")
    else:
        guide = ("Decide whether this change needs DESIGN DOCUMENTS in `design/` at the repository root (written for "
                 "the people who will own this code as much as for the team building it). A new program, a new part, "
                 "or a change to interfaces, data or layout does: write or update them. " + body + " A small fix, or a "
                 "change that fits the existing design as it is, does not: write none. Return `design` with `docs` "
                 "(every document in `design/` that applies to this request; empty when none does) and `why` (one "
                 "sentence).")
    listed = "\n".join(f"   - `{c['id']}`: `{c['command']}`" + (f" (covers: {'; '.join(c['covers'])})" if c.get("covers") else "")
                       for c in checks)
    have_checks = ("The repository already has these acceptance checks from earlier runs, in `.cc/checks.json` and "
                   f"`.cc/checks/`; they stay and run every round:\n{listed}\n   List in `checks` only checks that are new "
                   "or that this request changes (same `id` to replace one); put the ids of checks this request makes "
                   "wrong or obsolete in `retireChecks`." if checks else
                   "The repository has no acceptance checks yet.")
    existing = ""
    if docs or checks:
        existing = ("THIS REPOSITORY ALREADY HAS " + (f"a design ({', '.join(f'`{d}`' for d in docs[:20])})" if docs else "no design")
                    + f" and {len(checks)} acceptance check(s) from earlier runs, which stay and run every round. A team "
                    "designs only what this request ADDS OR CHANGES: list in `parts` only the subsystems whose design must "
                    "change, each architect revising its part's existing document. When the request continues or finishes "
                    "work the existing design already covers (a follow-up, a retry, \"finish what is left\"), return `team` "
                    "false: you plan alone, from that design and from which checks fail (below). Never "
                    "re-design or re-propose what exists (a follow-up that re-designed six existing parts and re-authored all "
                    "30 existing checks spent 30 minutes before building anything, run 0a717ffe).")
    return {"specGuide": spec, "designGuide": guide + existing_docs, "existingChecks": have_checks,
            "kickoffExisting": existing}


# ─────────────────────────────── the gate after planning ───────────────────────────────
def gate(work: str, plan: dict, specs: str, design: str, have: str) -> dict:
    """Did the architect deliver what this run's choices ask for? Proves the spec of the chosen kind,
    checks the design documents it names exist. ``{"ok", "kind", "why", "summary", "feedback", "brief",
    "designDocs", ...}``; ``feedback`` is what the architect reads on its next attempt."""
    choice = plan.get("spec") if isinstance(plan.get("spec"), dict) else {}
    kind = str(choice.get("kind") or "").strip().lower()
    why = str(choice.get("why") or "").strip()
    problems: list[str] = []
    if specs == "none":
        kind = "none"
    elif specs in ("tla", "openspec") and kind != specs:
        problems.append(f'The person asked for {A[specs]} spec: write it and return spec.kind "{specs}".')
    elif specs == "auto" and have and kind != have:
        problems.append(f"This repository has {A[have]} spec in `{WHERE[have]}`: revise it for this request "
                        f'(or leave it as it is) and return spec.kind "{have}"; it is never replaced or dropped.')
    elif kind not in KINDS:
        problems.append('Return `spec` with `kind` "tla", "openspec" or "none", and `why`.')
    d = plan.get("design") if isinstance(plan.get("design"), dict) else {}
    docs = [str(x).strip() for x in d.get("docs") or [] if str(x).strip()]
    if design == "none":
        docs = []
    else:
        # A part's document a design item owns is written by its designer after the plan (swarm/team.py).
        delegated = {p for it in plan.get("items") or [] if isinstance(it, dict)
                     and str(it.get("kind") or "").strip().lower() == "design"
                     for p in (it.get("owns") or []) if str(p).endswith(".md")}
        missing = [p for p in docs if not os.path.isfile(os.path.join(work, p)) and p not in delegated]
        if missing:
            problems.append("These design documents are named in `design.docs` but are not in the tree: "
                            + ", ".join(missing))
        if design == "always" and not docs:
            problems.append("The person asked for design documents: write them in `design/` and list them in "
                            "`design.docs`.")
    out = {"kind": kind, "why": why, "designDocs": docs, "designWhy": str(d.get("why") or "")}
    if problems:
        return {**out, "ok": False, "summary": "the plan does not give what this run asks for",
                "feedback": "\n".join(problems), "brief": ""}
    if kind == "tla":
        proof = formal.prove(work)
        out.update({k: proof.get(k) for k in ("states", "transitions", "traces", "mutants", "module", "missing")})
    elif kind == "openspec":
        proof = openspec_spec.prove(work, merge_checks(read_checks(work), plan))
        out.update({k: proof.get(k) for k in ("requirements", "scenarios", "missing")})
    else:
        proof = {"ok": True, "summary": f"no spec ({why or 'not needed'})", "feedback": ""}
    out.update(ok=bool(proof.get("ok")), summary=proof.get("summary") or "", feedback=proof.get("feedback") or "")
    out["brief"] = brief(kind, docs)
    return out


def brief(kind: str, docs: list[str]) -> str:
    """What every implementer, reviewer and the round review read about the spec and the design."""
    if docs:
        design = ("THE DESIGN: " + ", ".join(f"`{p}`" for p in docs) + " (read the documents that cover your part); "
                  "the team's brief with THE CONTRACT THE CHECKS USE is `.cc/design.md`.")
    else:
        design = "THE DESIGN: the team's brief with THE CONTRACT THE CHECKS USE is `.cc/design.md`."
    if kind == "tla":
        spec = ("THE SPEC (TLA+) in `.cc/spec/` is the mathematical definition of the behaviour: every action the "
                "program implements must do exactly what its operator in the .tla says, in its success and its failure "
                "cases, with exactly the result values it names. After every round the harness model-checks it "
                "(`spec-proof`) and replays the program through `.cc/conformance/adapter.mjs` against every transition "
                "of its bounded model (`spec-conformance`); a difference is reported as the shortest sequence of "
                "actions that shows it.")
    elif kind == "openspec":
        spec = ("THE SPEC (OpenSpec) in `.cc/openspec/specs/` states the requirements and their scenarios: build every "
                "requirement your part touches so each of its scenarios holds exactly as written. Every scenario is "
                "proven by the acceptance checks that cover it (`covers` in `.cc/checks.json`); after every round the "
                "harness validates the spec and its coverage (`spec-coverage`) and reports which requirements hold.")
    else:
        spec = "THE SPEC: none for this run; the acceptance checks state the behaviour."
    return design + "\n" + spec
