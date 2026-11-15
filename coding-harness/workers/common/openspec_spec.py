"""OpenSpec specs: requirements and scenarios the program must meet, each scenario proven by an acceptance
check (docs/FORMAL_SPEC_DESIGN.md, "OpenSpec").

The architect writes one file per capability, ``.cc/openspec/specs/<capability>/spec.md``, in the OpenSpec
format::

    ## Purpose
    <what the capability is for>

    ## Requirements
    ### Requirement: <name>
    The system SHALL ...

    #### Scenario: <name>
    - **WHEN** ...
    - **THEN** ...

and names, on every acceptance check, the scenarios it proves (``covers``). The harness checks this
mechanically, not by an agent's opinion. The files must be well formed, and each requirement must say
SHALL or MUST and have scenarios. Every scenario must be proven by at least one check, and every name a
check covers must be a real scenario. The checks themselves are proven red before the work, and run on
every committed tree, so a scenario holds exactly when its checks pass.

The spec and the checks persist in ``.cc/`` across runs: a later request edits the requirements, and every
scenario still has to be proven, so earlier behaviour stays checked.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.join(".cc", "openspec")
SPECS = os.path.join(ROOT, "specs")

_REQ = re.compile(r"^###\s+Requirement:\s*(.+?)\s*$")
_SCN = re.compile(r"^####\s+Scenario:\s*(.+?)\s*$")
_H2 = re.compile(r"^##\s+(.+?)\s*$")


def key(name: str) -> str:
    """How scenario names are matched: case and spacing do not matter."""
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def spec_files(workdir: str) -> list[str]:
    base = os.path.join(workdir, SPECS)
    if not os.path.isdir(base):
        return []
    return sorted(os.path.join(SPECS, d, "spec.md") for d in os.listdir(base)
                  if os.path.isfile(os.path.join(base, d, "spec.md")))


def parse(text: str, capability: str) -> tuple[list[dict], list[str]]:
    """``(requirements, problems)``: each requirement ``{capability, name, text, scenarios: [{name, when,
    then}]}``; problems name the file's format errors in words the architect can act on."""
    problems: list[str] = []
    sections: dict[str, list[str]] = {}
    current = ""
    for ln in text.splitlines():
        m = _H2.match(ln)
        if m and not ln.startswith("###"):
            current = m.group(1).strip().lower()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(ln)
    if not "".join(sections.get("purpose", [])).strip():
        problems.append(f"{capability}: no `## Purpose` section (one or two sentences on what the capability is for)")
    if "requirements" not in sections:
        problems.append(f"{capability}: no `## Requirements` section")
        return [], problems
    reqs: list[dict] = []
    req = scn = None
    for ln in sections["requirements"]:
        m = _REQ.match(ln)
        if m:
            req = {"capability": capability, "name": m.group(1), "text": "", "scenarios": []}
            reqs.append(req)
            scn = None
            continue
        m = _SCN.match(ln)
        if m:
            if req is None:
                problems.append(f"{capability}: scenario '{m.group(1)}' is not under a `### Requirement:`")
                continue
            scn = {"name": m.group(1), "when": False, "then": False}
            req["scenarios"].append(scn)
            continue
        if scn is not None:
            scn["when"] = scn["when"] or "**WHEN**" in ln
            scn["then"] = scn["then"] or "**THEN**" in ln
        elif req is not None:
            req["text"] += ln + "\n"
    if not reqs:
        problems.append(f"{capability}: `## Requirements` has no `### Requirement:`")
    for r in reqs:
        where = f"{capability} / requirement '{r['name']}'"
        if not re.search(r"\b(SHALL|MUST)\b", r["text"]):
            problems.append(f"{where}: its statement must say SHALL or MUST")
        if not r["scenarios"]:
            problems.append(f"{where}: has no `#### Scenario:`")
        for s in r["scenarios"]:
            if not (s["when"] and s["then"]):
                problems.append(f"{where} / scenario '{s['name']}': needs a `- **WHEN** ...` and a `- **THEN** ...` line")
    return reqs, problems


def read(workdir: str) -> tuple[list[dict], list[str]]:
    reqs: list[dict] = []
    problems: list[str] = []
    for rel in spec_files(workdir):
        capability = os.path.basename(os.path.dirname(rel))
        with open(os.path.join(workdir, rel), encoding="utf-8") as fh:
            r, p = parse(fh.read(), capability)
        reqs += r
        problems += p
    seen: dict[str, str] = {}
    for r in reqs:
        for s in r["scenarios"]:
            k = key(s["name"])
            if k in seen:
                problems.append(f"scenario '{s['name']}' appears twice ({seen[k]} and {r['capability']} / "
                                f"{r['name']}): give every scenario its own name, the checks cover it by name")
            seen[k] = f"{r['capability']} / {r['name']}"
    return reqs, problems


def prove(workdir: str, checks: list[dict]) -> dict:
    """Is the spec well formed, and is every scenario proven by a check? ``checks`` are the acceptance
    checks with their ``covers``. ``{"ok", "summary", "feedback", "requirements", "scenarios",
    "coverage": {scenario name: [check ids]}}``."""
    files = spec_files(workdir)
    if not files:
        return {"ok": False, "missing": True, "summary": "no OpenSpec spec",
                "feedback": f"No OpenSpec spec: write {SPECS}/<capability>/spec.md (see the format above)."}
    reqs, problems = read(workdir)
    names = {key(s["name"]): s["name"] for r in reqs for s in r["scenarios"]}
    coverage: dict[str, list[str]] = {n: [] for n in names.values()}
    for c in checks or []:
        for name in c.get("covers") or []:
            k = key(name)
            if k in names:
                coverage[names[k]].append(str(c.get("id")))
            else:
                problems.append(f"check '{c.get('id')}' covers '{name}', which is not a scenario in the spec")
    uncovered = [n for n, ids in coverage.items() if not ids]
    if uncovered:
        problems.append("no check proves these scenarios (name each in the `covers` of the check that drives it, "
                        "or add a check): " + "; ".join(f"'{n}'" for n in uncovered))
    n_scn = sum(len(r["scenarios"]) for r in reqs)
    result = {"requirements": [{"capability": r["capability"], "name": r["name"],
                                "scenarios": [s["name"] for s in r["scenarios"]]} for r in reqs],
              "scenarios": n_scn, "coverage": coverage, "files": files}
    if problems:
        return {**result, "ok": False, "summary": f"{len(problems)} problem(s) in the OpenSpec spec",
                "feedback": "The OpenSpec spec is not ready:\n" + "\n".join(f"- {p}" for p in problems[:40])}
    return {**result, "ok": True, "feedback": "",
            "summary": (f"{len(reqs)} requirement(s) in {len(files)} capability spec(s); all {n_scn} scenarios "
                        f"are proven by acceptance checks")}


def status(proof: dict, results: list[dict]) -> str:
    """Which requirements hold on a measured tree: each scenario holds when every check covering it
    passed. Readable by the architect and the console."""
    exit_by_id = {str(r.get("id")): r.get("exitCode") for r in results or []}
    lines = []
    for r in proof.get("requirements") or []:
        bad = []
        for s in r["scenarios"]:
            ids = (proof.get("coverage") or {}).get(s) or []
            failing = [i for i in ids if exit_by_id.get(i) != 0]
            if failing:
                bad.append(f"'{s}' (check {', '.join(failing)} not green)")
        lines.append(f"- {r['capability']} / {r['name']}: " + ("holds" if not bad else "NOT MET: " + "; ".join(bad)))
    return "\n".join(lines)
