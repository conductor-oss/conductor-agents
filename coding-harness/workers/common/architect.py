"""The Architect: the program's definition of done, its score, and the convergence ladder (pure).

A program used to prove itself only against the acceptance tests its own agents wrote: the
``resp-reader`` test of the redis program was wrong in a way nobody could correct, two tests did not
check what they claimed, and "RESP compliant" was never checked against anything real (runs 688f426b,
373cac75). The Architect turns the request into an **outcome contract** — checks with weights, the
agent-authored acceptance tests and the integration suite among them, plus checks the builders did
not write (a real client suite, a scenario, a budget, a docs check) found by scouts and **red-proved**
against the untouched head — and scores the integrated branch against it: the weighted share that
passes, reproducible for the same head and revision. Everything an Architect session decides is a
fact on the program board (``outcome_contract``, ``check_result``, ``evaluation``, ``amendment``,
``override``, ``opinion``, ``steer``, ``need``), so the role survives worker reloads and continuations.

The ladder is the first principle of the swarm: the goal is never to find a reason to stop. A score
under the threshold takes, in order and automatically, ``rework`` (a repair team, one tier stronger
each round), ``change_approach`` (a different plan; a repeat is rejected), ``second_opinion`` (an
independent judgment that may override, amend or send back — once per contract revision) and only
then ``needs``: a precisely named need (budget, an owner decision) the run ends on and a continuation
resumes from, at the same rung. Every round must raise the score or the rung advances. Nothing waits
on a human: steer is a fact the Architect reads at its next evaluation.

Pure functions only; the thin ``@worker_task`` wrappers live in ``stages/tasks.py``.
"""

from __future__ import annotations

import json
import re
import shlex

from common.shapes import lines as _lines

# ─────────────────────────── fact kinds (the Architect's state) ───────────────
CONTRACT_KIND = "outcome_contract"
RESULT_KIND = "check_result"
EVALUATION_KIND = "evaluation"
AMENDMENT_KIND = "amendment"
OVERRIDE_KIND = "override"
OPINION_KIND = "opinion"
STEER_KIND = "steer"
NEED_KIND = "need"

# Check kinds. ``acceptance`` and ``integration`` are the swarm's own checks (always in the
# contract); the others are external: found by scouts, never written by the builders.
SCOUT_KINDS = ("client", "scenario", "budget", "docs")
OWN_KINDS = ("acceptance", "integration")
KINDS = OWN_KINDS + SCOUT_KINDS

# Ladder rungs, in order. ``deliver`` and ``needs`` end the loop; ``amend`` and ``replan`` are one
# extra pass (a reviewer decides a pending amendment; a repeated plan is sent back one tier up).
RUNGS = ("rework", "change_approach", "second_opinion")
DELIVER, NEEDS, AMEND, REPLAN = "deliver", "needs", "amend", "replan"

DEFAULT_THRESHOLD = 1.0
DEFAULT_WEIGHTS = {"acceptance": 3, "integration": 3, "client": 3, "scenario": 2, "budget": 1, "docs": 1}
# Where the contract and its check scripts live on the program branch (frozen for every team).
CONTRACT_DIR = ".cc"
CONTRACT_FILE = ".cc/contract.json"
CHECK_ROOT = ".cc/checks"
# Where scouts may install tools: inside the worktree, never the repository tree.
CACHE_ROOT = ".cc-cache"

_PLACEHOLDER = {"test", "todo", "tbd", "placeholder", "ok", "x", "n/a", "none", "tba", "fixme", "...", ""}


def _mapping(v) -> dict:
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def _listed(v) -> list:
    return v if isinstance(v, list) else []


def _num(v, default: float = 0.0) -> float:
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _placeholder(text) -> bool:
    return str(text or "").strip().strip(".").lower() in _PLACEHOLDER


def _slug(text: str) -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in str(text).strip().lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:40] or "check"


# ─────────────────────────── schemas ──────────────────────────────────────────
# What the define session (the program planner) adds to its plan: the draft of the definition of done.
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "threshold": {"type": "number", "description": "the weighted share of checks that must pass for the "
                                                       "program to be delivered; 1.0 unless a budget or docs "
                                                       "check is advisory"},
        "checks": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(SCOUT_KINDS)},
            "claim": {"type": "string", "description": "the claim of the request this check proves, in the "
                                                       "request's own words"},
            "weight": {"type": "integer", "minimum": 1, "maximum": 5},
            "hint": {"type": "string", "description": "what a scout should look for: a client, a conformance "
                                                      "suite, a scenario, a budget, a docs check"},
            "command": {"type": "string", "description": "the one argv line that runs the check from the "
                                                         "repository root (no shell operators), e.g. "
                                                         "`node .cc/checks/scenario.mjs`; with 'script', required"},
            "setup": {"type": "array", "items": {"type": "string"},
                      "description": "argv lines that install what the command needs under .cc-cache/ "
                                     "(idempotent; run before every check run)"},
            "interface": {"type": "string", "description": "the exact user-facing interface the check drives, "
                                                           "in one paragraph: invocation, ports, paths, labels, "
                                                           "expected output"},
            "script": {"type": "object", "description": "the check's script, WRITTEN NOW by the Architect: a "
                                                        "path under .cc/checks/ (.mjs, .cjs, .sh or .py, never "
                                                        ".js) and its full content. A check with a script needs "
                                                        "no scout: it is committed before the first team forks",
                       "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]},
        }, "required": ["kind", "claim"]}},
    },
}


def authored_checks(draft: dict) -> list[dict]:
    """The draft's checks the Architect wrote itself (a script under .cc/checks/ and the command that
    runs it), in the shape a scout returns, so the fold treats them exactly like a scout's candidate.
    Why: the scenario scout ran beside the first implementer and landed its check after the implementer
    had started, so the reviewer sent a correct build back for a whole rework round in every run
    (b00146a9: heading text and an svg path; 74bd988c: an Evaluate button). A check the planner writes
    is on the branch before the first team forks and is in its implementer's brief from the start."""
    out: list[dict] = []
    for o in _listed(_mapping(draft).get("checks")):
        o = _mapping(o)
        script = _mapping(o.get("script"))
        path = str(script.get("path") or "").strip()
        command = str(o.get("command") or "").strip()
        if not path or not str(script.get("content") or "").strip() or not command:
            continue
        if str(o.get("kind") or "") not in SCOUT_KINDS or _placeholder(o.get("claim")):
            continue
        out.append({"found": True, "kind": str(o["kind"]), "claim": str(o.get("claim") or ""), "command": command,
                    "setup": [str(s) for s in _listed(o.get("setup")) if str(s).strip()],
                    "weight": o.get("weight") or DEFAULT_WEIGHTS[str(o["kind"])],
                    "interface": str(o.get("interface") or ""), "paths": [path],
                    "script": {"path": path, "content": str(script.get("content"))}, "source": "architect"})
    return out

# What a scout returns: a runnable, red-proved check or an honest ``none found``.
SCOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "kind": {"type": "string", "enum": list(SCOUT_KINDS)},
        "claim": {"type": "string"},
        "command": {"type": "string", "description": "one argv line, run from the repository root; no shell "
                                                     "operators. Use a script under .cc/checks/ for anything "
                                                     "that needs a server, a build or several steps"},
        "setup": {"type": "array", "items": {"type": "string"},
                  "description": "argv lines that install what the command needs under .cc-cache/; "
                                 "idempotent, run before every check run"},
        "weight": {"type": "integer", "minimum": 1, "maximum": 5},
        "interface": {"type": "string", "description": "the exact interface the check drives, in one "
                                                       "paragraph: the command-line syntax or endpoints with "
                                                       "an example invocation and its expected output, the "
                                                       "ports, paths and formats. Every builder reads it as "
                                                       "the binding interface"},
        "observed": {"type": "string", "description": "the command's output against the untouched head"},
        "exitCode": {"type": "integer"},
        "need": {"type": "string", "description": "only when the check cannot run here for a reason outside "
                                                  "the swarm's reach (a credential, a licence): what is needed"},
        "reason": {"type": "string", "description": "when found is false: what was looked for and why nothing "
                                                    "runnable exists"},
    },
    "required": ["found"],
}

# The contract reviewer and the amendment reviewer: approve or reject, never an open-ended edit.
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "reject"]},
        "findings": {"type": "array", "items": {"type": "string"}},
        "weights": {"type": "array", "description": "on reject: the weight each named check should have (0 drops it)",
                    "items": {"type": "object", "properties": {"check": {"type": "string"},
                                                               "weight": {"type": "integer", "minimum": 0, "maximum": 5}},
                              "required": ["check", "weight"]}},
    },
    "required": ["verdict"],
}

ITEM_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string"},
    "subsystem": {"type": "string", "description": "the subsystem this item belongs to"},
    "description": {"type": "string"},
    "deliverable": {"type": "string", "enum": ["code", "docs"]},
    "files": {"type": "array", "items": {"type": "string"}},
    "dependsOn": {"type": "array", "items": {"type": "string"}},
    "acceptanceCriteria": {"type": "array", "items": {"type": "string"},
                           "description": "one observable criterion per entry"},
    "reviewCriteria": {"type": "array", "items": {"type": "string"}},
}, "required": ["id", "description", "files", "acceptanceCriteria"]}

# What the Architect's evaluation returns: items for the gaps, directives for teams still to build,
# amendments of the contract it now believes wrong.
EVALUATE_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {"type": "string", "description": "what the integrated branch does and does not do "
                                                        "against the contract, in a few sentences"},
        "items": {"type": "array", "items": ITEM_SCHEMA},
        "directives": {"type": "array", "items": {"type": "object", "properties": {
            "subsystem": {"type": "string"},
            "item": {"type": "string", "description": "the one item this is for, when it is not for the whole team"},
            "text": {"type": "string"}}, "required": ["subsystem", "text"]}},
        "amendments": {"type": "array", "items": {"type": "object", "properties": {
            "check": {"type": "string"},
            "op": {"type": "string", "enum": ["drop", "reweight", "threshold"]},
            "weight": {"type": "integer", "minimum": 0, "maximum": 5},
            "threshold": {"type": "number"},
            "reason": {"type": "string"}}, "required": ["op", "reason"]}},
    },
    "required": ["assessment", "items"],
}

# The second opinion: the work meets the requirement, the contract is wrong, or the work is short.
OPINION_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["meets", "contract_wrong", "short"]},
        "reasoning": {"type": "string"},
        "amendments": EVALUATE_SCHEMA["properties"]["amendments"],
        "items": {"type": "array", "items": ITEM_SCHEMA},
    },
    "required": ["verdict", "reasoning"],
}


# ─────────────────────────── the draft (define session) ───────────────────────
def draft_problems(draft: dict) -> list[str]:
    """Fatal defects of a contract draft, the way the decomposition gate judges items: a placeholder
    claim, an unknown kind, a threshold outside (0, 1]. An absent draft is not a defect (the swarm's
    own checks always make a contract)."""
    draft = _mapping(draft)
    problems: list[str] = []
    if not draft:
        return problems
    th = draft.get("threshold")
    if th is not None and not 0 < _num(th, -1) <= 1:
        problems.append(f"contract threshold {th!r} is not within (0, 1]")
    for n, o in enumerate(_listed(draft.get("checks"))):
        o = _mapping(o)
        if str(o.get("kind") or "") not in SCOUT_KINDS:
            problems.append(f"check {n + 1} has kind {o.get('kind')!r}; one of {', '.join(SCOUT_KINDS)}")
        if _placeholder(o.get("claim")):
            problems.append(f"check {n + 1} ({o.get('kind')}) has a placeholder claim "
                            f"({str(o.get('claim') or '').strip()!r}); state the claim of the request it proves")
        script = _mapping(o.get("script"))
        if script:
            path = str(script.get("path") or "").strip()
            if not path.startswith(CHECK_ROOT + "/") or path.endswith(".js") or "/../" in f"/{path}":
                problems.append(f"check {n + 1} ({o.get('kind')}) script path {path!r} must be under "
                                f"{CHECK_ROOT}/ and end in .mjs, .cjs, .sh or .py")
            if not str(script.get("content") or "").strip():
                problems.append(f"check {n + 1} ({o.get('kind')}) script {path!r} has no content")
            try:
                argv = check_argv(str(o.get("command") or ""))
            except ValueError as e:
                argv = []
                problems.append(f"check {n + 1} ({o.get('kind')}) command: {e}")
            if argv and path and path not in argv:
                problems.append(f"check {n + 1} ({o.get('kind')}) command {o.get('command')!r} does not run its "
                                f"script {path!r}")
    return problems


def scout_briefs(draft: dict, *, external: bool = True) -> list[dict]:
    """One scout per external kind the draft asks for (every kind when it asks for none): the kind,
    the claims to prove and the planner's hints. Empty when external checks are off."""
    if not external:
        return []
    by_kind: dict[str, list[dict]] = {}
    authored = {str(_mapping(a).get("claim")) for a in authored_checks(draft)}
    asked = 0
    for o in _listed(_mapping(draft).get("checks")):
        o = _mapping(o)
        if str(o.get("kind") or "") in SCOUT_KINDS and not _placeholder(o.get("claim")):
            asked += 1
            # A check the Architect wrote itself needs no scout (``authored_checks``).
            if str(o.get("claim")) not in authored:
                by_kind.setdefault(str(o["kind"]), []).append(o)
    kinds = [k for k in SCOUT_KINDS if k in by_kind] or ([] if asked else list(SCOUT_KINDS))
    return [{"kind": k, "claims": [str(o.get("claim")) for o in by_kind.get(k, [])],
             "hints": [str(o.get("hint")) for o in by_kind.get(k, []) if str(o.get("hint") or "").strip()],
             "weight": max([int(_num(o.get("weight"), 0)) for o in by_kind.get(k, [])] or [0])
             or DEFAULT_WEIGHTS[k]} for k in kinds]


# ─────────────────────────── the contract (fold, red-proof) ───────────────────
def _check(*, oid: str, kind: str, claim: str, weight, command: str = "", setup=None, state: str = "red",
            mechanism: str = "", source: str = "scout", need: str = "", paths=None, interface: str = "") -> dict:
    return {"id": oid, "kind": kind, "claim": str(claim or "").strip(), "weight": max(int(_num(weight, 1)), 0),
            "command": str(command or "").strip(), "setup": [str(s) for s in _listed(setup) if str(s).strip()],
            "state": state, "mechanism": str(mechanism or ""), "source": source, "need": str(need or ""),
            "paths": [str(p) for p in _listed(paths) if str(p).strip()], "interface": str(interface or "").strip()}


def own_checks(weights: dict | None = None) -> list[dict]:
    """The two checks every program carries: its teams' frozen acceptance tests and the
    repository's integration suite. Both are red before any work: no test exists yet."""
    w = {**DEFAULT_WEIGHTS, **_mapping(weights)}
    return [
        _check(oid="acceptance", kind="acceptance", claim="every team's frozen acceptance tests pass at the "
                "integrated head", weight=w["acceptance"], source="swarm",
                mechanism="no acceptance test exists before the teams build"),
        _check(oid="integration", kind="integration", claim="the repository's own test suite passes at the "
                "integrated head", weight=w["integration"], source="swarm",
                mechanism="the integration suite has not run"),
    ]


_TOOLING_FAILURE = re.compile(
    r"(?:ERR_MODULE_NOT_FOUND|Cannot find (?:module|package)|MODULE_NOT_FOUND|ModuleNotFoundError|"
    r"ImportError: No module named|ERR_REQUIRE_ESM).*?['\"]([^'\"]+)['\"]")
_TOOL_PACKAGES = ("playwright", "@playwright/test", "puppeteer", "selenium-webdriver", "requests", "httpx", "redis")


def tooling_failure(text: str) -> str:
    """The line on which a check's OWN tooling failed to load (a module under the tool cache or
    node_modules, or a known client package), or "". A missing PROGRAM file is a legitimate red
    (`./calc: No such file` before the build exists) and is never matched. A scenario that imported
    Playwright from a path one directory off was red-proved as a valid check and then failed every
    delivery for a reason no implementer could fix, three repair sessions long (run 997b9d97)."""
    for line in str(text or "").splitlines():
        m = _TOOLING_FAILURE.search(line)
        if not m:
            continue
        ref = m.group(1)
        if ".cc-cache" in ref or "/node_modules/" in ref or ref in _TOOL_PACKAGES or ref.split("/")[0] in _TOOL_PACKAGES:
            return line.strip()[:300]
    return ""


def red_proof(candidate: dict, run: dict | None) -> dict:
    """Judge one scout candidate by its run against the untouched head: ``{"state": red|blocked|
    vacuous|unsound, "mechanism"}``. ``run`` is ``{exitCode, output, error}``; no run means blocked.
    A pass is vacuous (the check would pass with no work done) and a script whose own tooling failed
    to load is unsound (``tooling_failure``); neither enters the contract. Whether a script CAN pass
    is proved by running it, never by reading it: a static rule against backgrounding the program
    under test dropped a valid psql scenario and declared a run delivered without it (run 1cabdcac)."""
    candidate = _mapping(candidate)
    if not str(candidate.get("command") or "").strip():
        return {"state": "blocked", "mechanism": "no command"}
    if candidate.get("need"):
        return {"state": "blocked", "mechanism": f"needs {candidate['need']}"}
    run = _mapping(run)
    if not run or run.get("error"):
        return {"state": "blocked", "mechanism": str(run.get("error") or "the command was not run")}
    if int(_num(run.get("exitCode"), 1)) == 0:
        return {"state": "vacuous", "mechanism": "vacuous: passed before any work"}
    broken = tooling_failure(str(run.get("output") or ""))
    if broken:
        return {"state": "unsound", "mechanism": f"the check's own tooling failed, not the program: {broken}"}
    return {"state": "red", "mechanism": ""}


def fold_contract(draft: dict, scouts: list, runs: dict | None = None, *, external: bool = True,
                  weights: dict | None = None) -> dict:
    """Revision 1 of the contract from the draft, the scouts' candidates and their red-proof runs
    (keyed by candidate command): a vacuous candidate is dropped with its mechanism, a candidate whose
    setup failed stays ``blocked`` (red in the score, never passed), a kind no scout could serve is
    recorded as absent with the scout's reasoning. The swarm's own two checks are always present."""
    draft = _mapping(draft)
    runs = _mapping(runs)
    threshold = _num(draft.get("threshold"), DEFAULT_THRESHOLD)
    if not 0 < threshold <= 1:
        threshold = DEFAULT_THRESHOLD
    checks = own_checks(weights)
    dropped: list[dict] = []
    absent: dict[str, str] = {}
    served: set[str] = set()
    for n, s in enumerate(_listed(scouts)):
        s = _mapping(s)
        kind = str(s.get("kind") or "")
        if kind not in SCOUT_KINDS:
            continue
        if not s.get("found"):
            absent[kind] = str(s.get("reason") or "the scout found nothing runnable")
            continue
        served.add(kind)
        oid = f"{kind}-{_slug(s.get('claim') or kind)}"
        if any(o["id"] == oid for o in checks):
            oid = f"{oid}-{n + 1}"
        proof = red_proof(s, runs.get(str(s.get("command") or "").strip()))
        if proof["state"] in ("vacuous", "unsound"):
            dropped.append({"id": oid, "kind": kind, "command": str(s.get("command") or ""),
                            "mechanism": proof["mechanism"]})
            continue
        weight = s.get("weight") or _mapping(weights).get(kind) or DEFAULT_WEIGHTS[kind]
        checks.append(_check(oid=oid, kind=kind, claim=s.get("claim") or kind, weight=weight,
                               command=s.get("command"), setup=s.get("setup"), state=proof["state"],
                               mechanism=proof["mechanism"], need=s.get("need"), paths=s.get("paths"),
                               interface=s.get("interface")))
    if external:
        for kind in SCOUT_KINDS:
            if kind not in served and kind not in absent:
                absent[kind] = "no scout ran for this kind"
    else:
        for kind in SCOUT_KINDS:
            absent.setdefault(kind, "external checks are off for this program")
    return {"revision": 1, "threshold": threshold, "checks": checks, "absentKinds": absent,
            "dropped": dropped, "mechanism": ""}


def contract_paths(contract: dict) -> list[str]:
    """The files on the branch no team may edit: the contract file and every check script."""
    paths = [CONTRACT_FILE]
    for o in _listed(_mapping(contract).get("checks")):
        for p in check_scripts(o):
            if p not in paths:
                paths.append(p)
    return paths


def check_scripts(check: dict) -> list[str]:
    """The scripts one check runs: its recorded paths and the script its command names."""
    check = _mapping(check)
    out = [str(p) for p in _listed(check.get("paths")) if str(p).strip()]
    try:
        argv = check_argv(str(check.get("command") or ""))
    except ValueError:
        argv = []
    for tok in argv:
        if tok.startswith(CHECK_ROOT + "/") and tok not in out:
            out.append(tok)
    return out


_SCRIPT_CHARS = 3500
_BRIEF_CHARS = 14000
def contract_brief(contract: dict, scripts: dict | None = None) -> str:
    """The definition of done as every builder reads it: each external check's claim, the exact command
    that runs it, the interface it drives and the text of its scripts, then the swarm's own two checks.
    Four agents (planner, test author, implementer, reviewer) each guessed the calculator's CLI syntax
    while the scout's check drove ``3 + 4``; the Architect reconciled them by two repair rounds (run
    14b40b1a). Empty without external checks: the own checks are the item's criteria and tests."""
    c = _mapping(contract)
    scripts = _mapping(scripts)
    external = [_mapping(o) for o in _listed(c.get("checks")) if str(_mapping(o).get("kind")) not in OWN_KINDS]
    if not external:
        return ""
    lines = [f"DEFINITION OF DONE (contract revision {c.get('revision')}): the program is scored against these "
             f"checks at the integrated head. They drive the program EXACTLY as written below: match their "
             f"invocation syntax, arguments, paths, ports and output format. Where this item's description or "
             f"criteria and a check disagree on the interface, the check wins; say so in your summary. "
             f"The checks and their scripts are frozen: never edit them."]
    for o in external:
        lines.append(f"- {o.get('id')} [{o.get('kind')}, weight {o.get('weight')}]: {o.get('claim')}")
        if o.get("command"):
            lines.append(f"    command (from the repository root): {o.get('command')}")
        for s in _listed(o.get("setup")):
            lines.append(f"    setup: {s}")
        for p in check_scripts(o):
            text = str(scripts.get(p) or "")
            if text.strip():
                body = text[:_SCRIPT_CHARS] + ("\n      ... (truncated)" if len(text) > _SCRIPT_CHARS else "")
                lines.append(f"    script {p}:")
                lines.extend("      " + ln for ln in body.splitlines())
    for o in _listed(c.get("checks")):
        o = _mapping(o)
        if str(o.get("kind")) in OWN_KINDS:
            lines.append(f"- {o.get('id')} [{o.get('kind')}, weight {o.get('weight')}]: {o.get('claim')}")
    return "\n".join(lines)[:_BRIEF_CHARS]


def plan_text(subsystems: list) -> str:
    """The plan's items and their acceptance criteria, for the scouts: a check drives the program the
    way the plan says it is driven, and decides only what the plan leaves open."""
    rows = []
    for s in _listed(subsystems):
        s = _mapping(s)
        items = [_mapping(it) for it in _listed(s.get("items"))] or [s]
        for it in items:
            crit = "; ".join(_lines(it.get("acceptanceCriteria")))
            rows.append(f"- {s.get('id')}/{it.get('id')}: {str(it.get('description') or '')[:300]}"
                        + (f"\n    criteria: {crit[:800]}" if crit else ""))
    return "\n".join(rows)[:6000] or "(no items planned)"


def check_argv(command: str) -> list[str]:
    """The argv of a check command: one line, no shell operators (a script carries the rest)."""
    argv = shlex.split(str(command or ""))
    if not argv:
        raise ValueError("empty check command")
    bad = [t for t in argv if t in ("|", "||", "&&", ";", ">", ">>", "<", "&")]
    if bad:
        raise ValueError(f"shell operator {bad[0]!r} in a check command; put the steps in a script")
    return argv


# ─────────────────────────── the score ────────────────────────────────────────
def passing(check: dict, result: dict | None) -> bool:
    """A check passes when a human accepted it (``satisfied``) or its latest run exited 0. A
    ``blocked`` run, a missing run and a non-zero exit are all red: blocked is never a pass."""
    check = _mapping(check)
    if check.get("state") == "satisfied":
        return True
    result = _mapping(result)
    return bool(result) and result.get("state") == "green"


def score(contract: dict, results: dict) -> float:
    """``Σ weight(passing) / Σ weight(all)`` over the contract's checks, rounded to 4 places. The
    same head and revision give the same number. No checks (impossible: the swarm's own two are
    always present) scores 0."""
    checks = [_mapping(o) for o in _listed(_mapping(contract).get("checks"))]
    total = sum(int(_num(o.get("weight"), 0)) for o in checks)
    if total <= 0:
        return 0.0
    got = sum(int(_num(o.get("weight"), 0)) for o in checks if passing(o, _mapping(results).get(str(o.get("id")))))
    return round(got / total, 4)


def at_threshold(contract: dict, s: float) -> bool:
    return s >= _num(_mapping(contract).get("threshold"), DEFAULT_THRESHOLD) - 1e-9


def red_checks(contract: dict, results: dict) -> list[dict]:
    """The checks not passing, each with its latest result (output tail, mechanism), for a repair
    team's brief and the console."""
    out = []
    for o in _listed(_mapping(contract).get("checks")):
        o = _mapping(o)
        r = _mapping(_mapping(results).get(str(o.get("id"))))
        if not passing(o, r):
            out.append({**o, "result": r})
    return out


# ─────────────────────────── amendments ───────────────────────────────────────
def apply_amendment(contract: dict, amendment: dict, decision: str, *, by: str = "") -> dict:
    """The next contract revision when ``decision`` is ``approve``; the same contract (same revision)
    on ``reject``. Ops: ``drop`` a check, ``reweight`` it, ``satisfy`` it (a human accepted the
    item: it counts as passing), ``replace`` its command, ``threshold``. Unknown check ids change
    nothing. A ``weights`` map (the reviewer's reductions at the review bound) applies several at once."""
    contract = _mapping(contract)
    if str(decision) != "approve":
        return contract
    amendment = _mapping(amendment)
    op = str(amendment.get("op") or "")
    checks = [dict(_mapping(o)) for o in _listed(contract.get("checks"))]
    threshold = _num(contract.get("threshold"), DEFAULT_THRESHOLD)
    changed = False
    target = str(amendment.get("check") or "")
    if op == "threshold" and 0 < _num(amendment.get("threshold"), -1) <= 1:
        threshold, changed = _num(amendment.get("threshold")), True
    elif op == "weights":
        for oid, w in _mapping(amendment.get("weights")).items():
            for o in checks:
                # The swarm's own checks are never zeroed, by a reviewer's weights either: a review set
                # acceptance and integration to 0, and the program's own tests stopped counting (run a8167484).
                if o["kind"] in OWN_KINDS and int(_num(w, -1)) == 0:
                    continue
                if o["id"] == str(oid) and int(_num(w, -1)) >= 0 and o["weight"] != int(_num(w)):
                    o["weight"], changed = int(_num(w)), True
        checks = [o for o in checks if o["weight"] > 0 or o["kind"] in OWN_KINDS]
    else:
        for o in checks:
            if o["id"] != target:
                continue
            if op == "drop" and o["kind"] not in OWN_KINDS:
                o["weight"], changed = 0, True
            elif op == "reweight" and int(_num(amendment.get("weight"), -1)) >= 0:
                o["weight"], changed = int(_num(amendment.get("weight"))), True
            elif op == "satisfy":
                o["state"], o["mechanism"], changed = "satisfied", f"accepted by {by or 'a human'}: " \
                    f"{str(amendment.get('reason') or '')}".strip(), True
            elif op == "replace" and str(amendment.get("command") or "").strip():
                o["command"], o["setup"] = str(amendment["command"]).strip(), [
                    str(s) for s in _listed(amendment.get("setup"))]
                o["state"], o["mechanism"], changed = "red", "", True
        checks = [o for o in checks if o["weight"] > 0 or o["kind"] in OWN_KINDS]
    if not changed:
        return contract
    history = _listed(contract.get("history")) + [{"revision": int(contract.get("revision") or 1) + 1,
                                                   "op": op, "check": target, "by": by,
                                                   "reason": str(amendment.get("reason") or "")}]
    return {**contract, "revision": int(contract.get("revision") or 1) + 1, "threshold": threshold,
            "checks": checks, "history": history}


def amendment_problems(amendment: dict, contract: dict | None = None, decided: list | None = None) -> str:
    """Why a proposed amendment cannot be recorded (empty when it can). Given the contract, an own
    check (acceptance, integration) can be neither dropped nor zeroed: the Architect proposed dropping
    the red integration check five times, each approved by the reviewer and each a no-op, and the
    ladder looped for 24 minutes (run 3c84dd5d). Given the amendments already decided, the same
    (op, check) is not proposed twice for one revision."""
    a = _mapping(amendment)
    op = str(a.get("op") or "")
    if op not in ("drop", "reweight", "satisfy", "replace", "threshold", "weights"):
        return f"unknown amendment op {op!r}"
    if op != "threshold" and op != "weights" and not str(a.get("check") or "").strip():
        return "an amendment names the check it changes"
    if _placeholder(a.get("reason")) and op != "satisfy":
        return "an amendment states its reason"
    target = str(a.get("check") or "").strip()
    own = next((o for o in _listed(_mapping(contract).get("checks"))
                if str(_mapping(o).get("id")) == target and str(_mapping(o).get("kind")) in OWN_KINDS), None)
    if own and (op == "drop" or (op == "reweight" and int(_num(a.get("weight"), -1)) == 0)):
        return (f"the swarm's own check {target!r} cannot be dropped or zeroed; it is "
                f"{_mapping(own).get('state')} because: {_mapping(own).get('mechanism') or 'see the check'}")
    revision = int(_num(_mapping(contract).get("revision"), 1)) if contract else None
    facts = [_mapping(d) for d in _listed(decided)]
    verdicts = {str(d.get("id")): str(d.get("decision")) for d in facts if d.get("decision")}
    for d in facts:
        if (str(d.get("op")) == op and str(d.get("check") or "") == target and d.get("by") == "architect"
                and (revision is None or int(_num(d.get("revision"), revision)) == revision)):
            return (f"the same amendment ({op} {target}) was already proposed for revision {revision} "
                    f"({verdicts.get(str(d.get('id')), 'undecided')}); it changed nothing, so propose something else")
    return ""


# ─────────────────────────── the ladder ───────────────────────────────────────
def _streak(history: list[dict], rung: str, now: float) -> int:
    """Rounds on ``rung`` (the trailing run of evaluations that decided it) after which the score did
    not exceed the best seen before that round, counted from the latest. Progress resets it."""
    run = []
    for h in reversed(history):
        if str(h.get("rung")) == rung:
            run.append(_num(h.get("score"), 0.0))
        else:
            break
    run.reverse()  # scores at the time each round on this rung was decided
    streak = 0
    scores = run + [now]
    for k in range(len(scores) - 1, 0, -1):
        best_before = max(scores[:k])
        if scores[k] > best_before + 1e-9:
            break
        streak += 1
    return streak


def next_rung(history: list, s: float, threshold: float, max_repair_rounds: int, *, overridden: bool = False,
              budget_spent: bool = False, needs: list | None = None, opinion_used: bool = False,
              forced: str = "", pending_amendment: bool = False) -> str:
    """The ladder's decision after an evaluation (pure). ``history`` is the earlier final evaluations
    (``{rung, score, revision}``, oldest first). Progress keeps the rung; ``max_repair_rounds`` rounds
    without a higher score advance it; ``second_opinion`` runs once per revision; ``needs`` only for a
    spent budget or a need outside the swarm's reach. ``forced`` sends the ladder to a named rung (the
    second opinion's ``short`` verdict → ``change_approach``). A pending amendment is decided first."""
    if overridden or s >= _num(threshold, DEFAULT_THRESHOLD) - 1e-9:
        return DELIVER
    if pending_amendment:
        return AMEND
    if budget_spent or needs:
        return NEEDS
    if forced in RUNGS:
        return forced
    hist = [_mapping(h) for h in _listed(history) if str(_mapping(h).get("rung")) in RUNGS]
    if not hist:
        return RUNGS[0]
    cur = str(hist[-1]["rung"])
    if cur == "second_opinion":
        # The opinion itself decides what follows (override, amendment, send-back); when none of those
        # applied, nothing is left on the ladder.
        return NEEDS
    if _streak(hist, cur, s) < max(int(max_repair_rounds), 1):
        return cur
    nxt = RUNGS[RUNGS.index(cur) + 1] if RUNGS.index(cur) + 1 < len(RUNGS) else NEEDS
    if nxt == "second_opinion" and opinion_used:
        return NEEDS
    return nxt


def tier_for(history: list, rung: str) -> int:
    """The model tier the next repair round or Architect session runs at: one up per round already
    spent on the current rung (the help rule), from zero."""
    hist = [_mapping(h) for h in _listed(history)]
    n = 0
    for h in reversed(hist):
        if str(h.get("rung")) == rung:
            n += 1
        else:
            break
    return n


def same_approach(previous: list, items: list) -> bool:
    """A change of approach that re-states the previous plan is no change: same item ids, or the
    same files with the same criteria."""
    def key(it):
        it = _mapping(it)
        return (tuple(sorted(str(f) for f in _listed(it.get("files")))),
                tuple(sorted(_lines(it.get("acceptanceCriteria")))))
    prev = [_mapping(i) for i in _listed(previous)]
    new = [_mapping(i) for i in _listed(items)]
    if not prev or not new:
        return False
    if {str(i.get("id")) for i in prev} == {str(i.get("id")) for i in new}:
        return True
    return {key(i) for i in prev} == {key(i) for i in new}


def fold_findings(items: list, *, subsystems: list | None = None) -> tuple[list[dict], list[str]]:
    """The Architect's items the board can take (files and observable criteria, the placeholder rule
    of the decomposition gate) and why each rejected one was. A subsystem the program does not have
    falls back to the first subsystem that owns one of the item's files, else the first subsystem."""
    subs = [_mapping(s) for s in _listed(subsystems)]
    owners = {}
    for s in subs:
        for f in [*_listed(s.get("files")), *(f for it in _listed(s.get("items")) for f in _listed(_mapping(it).get("files")))]:
            owners.setdefault(str(f), str(s.get("id")))
    kept, rejected, seen = [], [], set()
    for it in _listed(items):
        it = _mapping(it)
        iid = str(it.get("id") or "").strip()
        files = [str(f).strip() for f in _listed(it.get("files")) if str(f).strip()]
        crit = [c for c in _lines(it.get("acceptanceCriteria")) if not _placeholder(c)]
        if not iid or iid in seen:
            rejected.append(f"item {iid or '(no id)'}: missing or duplicate id")
            continue
        if _placeholder(it.get("description")):
            rejected.append(f"item {iid}: placeholder description")
            continue
        if not files:
            rejected.append(f"item {iid}: no files; an item names every file it edits")
            continue
        if not crit:
            rejected.append(f"item {iid}: no observable acceptance criteria")
            continue
        seen.add(iid)
        sid = str(it.get("subsystem") or "").strip()
        if subs and sid not in {str(s.get("id")) for s in subs}:
            sid = next((owners[f] for f in files if f in owners), str(subs[0].get("id")))
        kept.append({**it, "id": iid, "files": files, "acceptanceCriteria": crit,
                     "reviewCriteria": _lines(it.get("reviewCriteria")),
                     "dependsOn": [str(d) for d in _listed(it.get("dependsOn")) if str(d).strip()],
                     "subsystem": sid})
    return kept, rejected


# ─────────────────────────── steer ────────────────────────────────────────────
STEER_SHAPES = ("feedback", "accept", "amend", "budget")


def steer_problems(shape: str, payload: dict) -> str:
    """Why a steer cannot be recorded (empty when it can)."""
    p = _mapping(payload)
    if shape not in STEER_SHAPES:
        return f"unknown steer {shape!r}; one of {', '.join(STEER_SHAPES)}"
    if shape == "feedback" and not str(p.get("text") or "").strip():
        return "feedback needs text"
    if shape == "accept" and not str(p.get("check") or p.get("item") or "").strip():
        return "accept names the check (or item) it accepts"
    if shape == "amend" and (not str(p.get("amendment") or "").strip()
                             or str(p.get("decision") or "") not in ("approve", "reject")):
        return "amend names the amendment and decides approve or reject"
    if shape == "budget" and _num(p.get("ceilingUsd"), 0) <= 0:
        return "budget needs a positive ceilingUsd"
    return ""


def fold_steer(facts: list, seen: set | None = None) -> dict:
    """The unseen steer facts, folded: ``feedback`` (text for the next Architect prompt), ``accept``
    (human amendments to record as approved), ``amend`` (decisions on pending amendments), ``budget``
    (the highest ceiling asked for), and the keys now seen."""
    seen = set(seen or ())
    out = {"feedback": [], "accepts": [], "decisions": {}, "ceilingUsd": 0.0, "seen": []}
    for f in _listed(facts):
        f = _mapping(f)
        key = str(f.get("key") or f.get("ts") or "")
        if key in seen:
            continue
        d = _mapping(f.get("data"))
        shape = str(d.get("shape") or "")
        by = str(f.get("author") or d.get("by") or "human")
        if shape == "feedback":
            out["feedback"].append({"by": by, "text": str(d.get("text") or ""), "ts": f.get("ts")})
        elif shape == "accept":
            out["accepts"].append({"op": "satisfy", "check": str(d.get("check") or d.get("item") or ""),
                                   "reason": str(d.get("text") or d.get("reason") or ""), "by": by})
        elif shape == "amend":
            out["decisions"][str(d.get("amendment") or "")] = {"decision": str(d.get("decision") or ""), "by": by}
        elif shape == "budget":
            out["ceilingUsd"] = max(out["ceilingUsd"], _num(d.get("ceilingUsd"), 0))
        out["seen"].append(key)
    return out


# ─────────────────────────── needs and the report ─────────────────────────────
def needs_of(contract: dict, results: dict, *, budget_spent: bool, spent: float, ceiling: float,
             owner_items: list | None = None) -> list[dict]:
    """What the program needs from outside the swarm, each precisely: ``budget`` (spent of ceiling),
    a check's own ``need`` (a credential, a licence), or an ``owner decision`` on the red items the
    ladder could not close."""
    out = []
    if budget_spent:
        out.append({"kind": "budget", "detail": f"${spent:.2f} of ${ceiling:.2f} spent; raise the ceiling "
                                                f"and continue", "spentUsd": round(spent, 4), "ceilingUsd": ceiling})
    for o in _listed(_mapping(contract).get("checks")):
        o = _mapping(o)
        if o.get("need") and not passing(o, _mapping(results).get(str(o.get("id")))):
            out.append({"kind": "credential", "detail": f"{o['id']}: {o['need']}", "check": o["id"]})
    if owner_items:
        out.append({"kind": "owner decision", "detail": "the ladder could not close: "
                    + "; ".join(f"{_mapping(x).get('id')}: {str(_mapping(x).get('mechanism') or _mapping(_mapping(x).get('result')).get('tail') or '')[:200]}"
                                for x in owner_items), "checks": [str(_mapping(x).get("id")) for x in owner_items]})
    return out


def program_report(*, delivered: bool, contract: dict, results: dict, s: float, evaluations: list,
                   needs: list, override: dict | None, done_count: int, total: int, final_head: str,
                   ranks: list) -> dict:
    """The honest program report: ``PROGRAM_DELIVERED`` with the checks it was proved against, or
    ``PROGRAM_NEEDS`` naming each need; never a graded failure."""
    checks = []
    for o in _listed(_mapping(contract).get("checks")):
        o = _mapping(o)
        r = _mapping(_mapping(results).get(str(o.get("id"))))
        checks.append({"id": o.get("id"), "kind": o.get("kind"), "claim": o.get("claim"), "weight": o.get("weight"),
                        "passed": passing(o, r), "state": "satisfied" if o.get("state") == "satisfied"
                        else (r.get("state") or o.get("state") or "red"),
                        "mechanism": r.get("mechanism") or o.get("mechanism") or "", "tail": str(r.get("tail") or "")[:600]})
    history = [{"rank": e.get("rank"), "score": e.get("score"), "rung": e.get("rung"), "revision": e.get("revision")}
               for e in _listed(evaluations)]
    rank_desc = "; ".join(f"rank {i}: {', '.join(r)}" for i, r in enumerate(ranks)) or "(none)"
    head = str(final_head or "")[:12]
    red = [o for o in checks if not o["passed"]]
    thr = _num(_mapping(contract).get("threshold"), DEFAULT_THRESHOLD)
    if delivered:
        proved = ", ".join(f"{o['id']} ({'accepted' if o['state'] == 'satisfied' else 'green'})" for o in checks)
        how = (f"by override: {str(_mapping(override).get('reasoning') or '')[:300]}" if override
               else f"score {s:.2f} at threshold {thr:.2f}")
        report = (f"PROGRAM_DELIVERED: {done_count}/{total} subsystems built in dependency order ({rank_desc}); "
                  f"{how}; proved against {proved or 'no check'}. Head {head}.")
        return {"programDelivered": True, "verdict": "PROGRAM_DELIVERED", "report": report, "score": s,
                "threshold": thr, "checks": checks, "scoreHistory": history, "needs": [], "override": override}
    need_desc = "; ".join(f"{_mapping(n).get('kind')}: {_mapping(n).get('detail')}" for n in _listed(needs)) \
        or "owner decision: see the red checks"
    report = (f"PROGRAM_NEEDS: score {s:.2f} of threshold {thr:.2f} at head {head}; {done_count}/{total} "
              f"subsystems built ({rank_desc}). Needs — {need_desc}. Red: "
              + ("; ".join(f"{o['id']}: {o['mechanism'] or o['tail'][:160] or 'failed'}" for o in red) or "none")
              + ". The work on the branch stays; Continue resumes at the recorded rung.")
    return {"programDelivered": False, "verdict": "PROGRAM_NEEDS", "report": report, "score": s, "threshold": thr,
            "checks": checks, "scoreHistory": history, "needs": _listed(needs), "override": None}


# ─────────────────────────── prompt contexts ──────────────────────────────────
def contract_text(contract: dict, results: dict | None = None) -> str:
    """The contract as an agent reads it: one line per check with weight, state and the latest run."""
    c = _mapping(contract)
    lines = [f"revision {c.get('revision')}, delivery threshold {_num(c.get('threshold'), DEFAULT_THRESHOLD):.2f}"]
    for o in _listed(c.get("checks")):
        o = _mapping(o)
        r = _mapping(_mapping(results).get(str(o.get("id")))) if results is not None else {}
        st = "satisfied" if o.get("state") == "satisfied" else (r.get("state") or o.get("state") or "red")
        line = f"- {o.get('id')} [{o.get('kind')}, weight {o.get('weight')}] {st}: {o.get('claim')}"
        if o.get("command"):
            line += f"\n    command: {o.get('command')}"
        if o.get("interface"):
            line += f"\n    interface: {str(o.get('interface'))[:600]}"
        mech = r.get("mechanism") or o.get("mechanism")
        if mech:
            line += f"\n    mechanism: {str(mech)[:300]}"
        if r.get("tail"):
            line += f"\n    latest output: {str(r.get('tail'))[-700:]}"
        lines.append(line)
    for kind, why in _mapping(c.get("absentKinds")).items():
        lines.append(f"- (no {kind} check: {why})")
    return "\n".join(lines)


def evaluations_text(evaluations: list) -> str:
    rows = []
    for e in _listed(evaluations):
        e = _mapping(e)
        rows.append(f"- {e.get('rank')}: score {_num(e.get('score')):.2f} (revision {e.get('revision')}), "
                    f"rung {e.get('rung') or '-'}, items filed {len(_listed(e.get('items')))}"
                    + (f"; rejected: {'; '.join(str(x) for x in _listed(e.get('rejected')))}" if e.get("rejected") else ""))
    return "\n".join(rows) or "(none yet)"


def items_text(items: list) -> str:
    return json.dumps([{k: _mapping(i).get(k) for k in ("id", "subsystem", "description", "files", "acceptanceCriteria")}
                       for i in _listed(items)], default=str)[:12000] or "[]"


# ─────────────────────────── board readers ────────────────────────────────────
# Facts written before the term was renamed (runs started on 2026-11-09) used these keys and kinds.
_LEGACY_KEYS = {"oracles": "checks", "oracle": "check", "oracleRoot": "checkRoot"}
_LEGACY_KINDS = {RESULT_KIND: "oracle_result"}


def _plain(data: dict) -> dict:
    """A fact's data with the renamed keys read under their current names."""
    out = {}
    for k, v in _mapping(data).items():
        out[_LEGACY_KEYS.get(str(k), str(k))] = v
    return out


def _facts(cid: str, kind: str) -> list[dict]:
    from common import blackboard as bb
    if not cid:
        return []
    recs = list(bb.read_facts(cid, kind=kind))
    if kind in _LEGACY_KINDS:
        recs += bb.read_facts(cid, kind=_LEGACY_KINDS[kind])
        recs.sort(key=lambda f: float(f.get("ts") or 0))
    return [{**_plain(f.get("data")), "ts": f.get("ts"), "by": f.get("author")} for f in recs]


def contract_of(cid: str) -> dict:
    facts = _facts(cid, CONTRACT_KIND)
    return facts[-1] if facts else {}


def evaluations_of(cid: str, stage: str | None = None) -> list[dict]:
    return [e for e in _facts(cid, EVALUATION_KIND) if stage is None or e.get("stage") == stage]


def results_of(cid: str) -> dict:
    """The latest result per check id."""
    out: dict = {}
    for r in _facts(cid, RESULT_KIND):
        out[str(r.get("check"))] = r
    return out


def amendments_of(cid: str) -> dict:
    """Amendments folded by id: the latest fact per id (a decision supersedes the proposal)."""
    out: dict = {}
    for a in _facts(cid, AMENDMENT_KIND):
        aid = str(a.get("id") or "")
        out[aid] = {**out.get(aid, {}), **a}
    return out


def pending_amendments(cid: str) -> list[dict]:
    return [a for a in amendments_of(cid).values() if not a.get("decision")]


def override_of(cid: str, revision: int) -> dict:
    return next((o for o in reversed(_facts(cid, OVERRIDE_KIND)) if int(_num(o.get("revision"))) == revision), {})


def opinion_of(cid: str, revision: int) -> dict:
    return next((o for o in reversed(_facts(cid, OPINION_KIND)) if int(_num(o.get("revision"))) == revision), {})


def seen_steer(cid: str) -> set:
    seen: set = set()
    for e in evaluations_of(cid):
        seen |= {str(k) for k in _listed(e.get("steerSeen"))}
    return seen


def steer_facts(cid: str) -> list[dict]:
    from common import blackboard as bb
    return [{"key": f"{f.get('ts')}:{f.get('author')}", "ts": f.get("ts"), "author": f.get("author"),
             "data": _plain(f.get("data"))} for f in bb.read_facts(cid, kind=STEER_KIND)] if cid else []


def needs_facts(cid: str) -> list[dict]:
    return [n for n in _facts(cid, NEED_KIND)]


def _program_fact(cid: str) -> dict:
    from common import blackboard as bb
    facts = bb.read_facts(cid, kind="program")
    return _mapping(facts[-1].get("data")) if facts else {}


def read_contract(worktree: str) -> dict:
    """The definition of done as committed on the branch (``.cc/contract.json``); {} when absent."""
    import os
    path = os.path.join(str(worktree or ""), CONTRACT_FILE)
    if not worktree or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return _mapping(json.load(fh))
    except (OSError, ValueError):
        return {}


def external_checks(contract: dict) -> list[dict]:
    """The contract's external checks still to be proved (a scout's or the Architect's own), never the
    swarm's two own checks and never one marked satisfied."""
    return [_mapping(o) for o in _listed(_mapping(contract).get("checks"))
            if str(_mapping(o).get("kind")) in SCOUT_KINDS and _mapping(o).get("state") != "satisfied"
            and str(_mapping(o).get("command") or "").strip()]


def read_contract_brief(worktree: str) -> str:
    """The definition of done as committed on the branch (``.cc/contract.json`` and its scripts), as
    the brief every builder's prompt carries. The branch is the wall: a team reads the contract from
    its checkout, never from a second copy handed down. Empty when the branch holds no contract."""
    import os
    wt = str(worktree or "")
    path = os.path.join(wt, CONTRACT_FILE)
    if not wt or not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            contract = json.load(fh)
    except (OSError, ValueError):
        return ""
    scripts = {}
    for o in _listed(_mapping(contract).get("checks")):
        for rel in check_scripts(o):
            full = os.path.join(wt, rel)
            if os.path.isfile(full):
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        scripts[rel] = fh.read()
                except OSError:
                    continue
    return contract_brief(contract, scripts)


def _post_contract(cid: str, contract: dict, *, by: str = "architect", **extra) -> None:
    from common import blackboard as bb
    bb.post_fact(cid, by, CONTRACT_KIND, {**contract, **extra})


def board_view(cid: str) -> dict:
    """What the console and the TUI show: the contract, each check's latest result, the score history,
    the current rung, pending amendments, overrides and open needs. Empty without a contract."""
    contract = contract_of(cid)
    if not contract:
        return {}
    results = results_of(cid)
    evals = evaluations_of(cid)
    last = evals[-1] if evals else {}
    checks = []
    for o in _listed(contract.get("checks")):
        o = _mapping(o)
        r = _mapping(results.get(str(o.get("id"))))
        checks.append({"id": o.get("id"), "kind": o.get("kind"), "claim": o.get("claim"), "weight": o.get("weight"),
                        "command": o.get("command"), "passed": passing(o, r),
                        "state": "satisfied" if o.get("state") == "satisfied" else (r.get("state") or o.get("state") or "red"),
                        "mechanism": r.get("mechanism") or o.get("mechanism") or "", "tail": str(r.get("tail") or "")[-700:],
                        "seconds": r.get("seconds"), "head": r.get("head")})
    return {"revision": contract.get("revision"), "threshold": contract.get("threshold"), "checks": checks,
            "absentKinds": _mapping(contract.get("absentKinds")), "dropped": _listed(contract.get("dropped")),
            "score": last.get("score"), "rung": last.get("rung") or "", "stage": last.get("stage") or "",
            "history": [{"stage": e.get("stage"), "rank": e.get("rank"), "score": e.get("score"), "rung": e.get("rung"),
                         "revision": e.get("revision"), "ts": e.get("ts"), "items": len(_listed(e.get("items")))} for e in evals],
            "pendingAmendments": [{"id": a.get("id"), "op": a.get("op"), "check": a.get("check"), "reason": a.get("reason"),
                                   "by": a.get("by")} for a in pending_amendments(cid)],
            "amendments": [{"id": a.get("id"), "op": a.get("op"), "check": a.get("check"), "reason": a.get("reason"),
                            "decision": a.get("decision"), "by": a.get("by")} for a in amendments_of(cid).values()],
            "override": override_of(cid, int(_num(contract.get("revision"), 1))) or None,
            "needs": [{"kind": n.get("kind"), "detail": n.get("detail")} for n in needs_facts(cid)
                      if int(_num(n.get("evaluation"), -1)) == len(evals)],
            "copiedFrom": contract.get("copiedFrom") or ""}
