"""The verification gate: independent verifiers look at a build the checks call done, once per run.

Every layer before it judges against the checks one architect wrote from its reading of the request, so a
misreading of the request passes them all. The gate adds independent readers, and keeps them from
becoming noise or a crowd:

* it runs once per run, when everything is green and the lead would deliver; at most ``MAX_VERIFIERS``
  verifiers, each looking for one thing (``FOCUSES``), each in its own throwaway copy of the tree (what
  it writes there never reaches the run's tree); the request-fidelity verifier's copy has no design and
  no checks, so it cannot inherit the lead's reading;
* a finding blocks delivery only when the HARNESS reproduces it: the verifier's script, in the check
  format, run once on a clean copy of the committed tree, must fail (a weak-check finding: pass on the
  tree, fail on the verifier's mutated copy, which the named check still passes); the agent's word
  never counts;
* it must quote the requirement it breaks, and the quote must be in the request, the spec or the seams;
  a finding that quotes nothing of the sort goes to follow-up, not this run;
* findings on the same requirement are merged; one the lead ruled on before (``.cc/findings.json``) is
  not raised again; at most ``MAX_NOTES`` notes per verifier, which never become work;
* the lead rules on each standing finding (fix, follow-up, reject); a fix makes the reproduction a frozen
  check, so the ordinary rounds prove the fix once and it can never regress; the gate never runs again.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil

FOCUSES = {
    "fidelity": ("REQUEST FIDELITY. Use the delivered product the way the person who wrote the request would, and "
                 "find where it does not do what the request asks. You see only the request and the product: no "
                 "design, no checks (that is deliberate: you are the reader who did not make the plan)."),
    "seams": ("THE SEAMS. Drive end-to-end flows that cross the parts (the seams are in `.cc/design.md`, the parts in "
              "`design/architecture.md`) and find where two parts disagree: a shape, a name, an order, an error one "
              "side sends and the other does not handle."),
    "checks": ("CHECK STRENGTH. Find an acceptance check (in `.cc/checks.json`) that a wrong implementation would "
               "still pass: make a small change to the program that breaks a requirement the check is meant to "
               "prove while the check stays green. Your finding carries that change (`mutation`, a unified diff "
               "against this tree), the check it slips past (`check`), and a stronger check (`script`/`command`) "
               "that passes on this tree and fails on your mutated one."),
    "security": ("SECURITY. Find where the product does something the request rules out: data or actions reachable "
                 "by someone who must not reach them, input accepted that must be refused, secrets exposed."),
    "data": ("DATA SAFETY. Find where the product loses or corrupts what the request cares about: a destroy or "
             "delete path, a migration, a restart, two writers at once."),
    "performance": ("PERFORMANCE. Only what the request asks for: find where the product misses a speed, size or "
                    "load the request states."),
}
DEFAULT_ORDER = ("fidelity", "seams", "checks")
MAX_VERIFIERS = 4
MAX_NOTES = 3
MAX_REPRODUCTIONS = 12
MIN_QUOTE = 12
STALL_AFTER_S = 120
LEDGER = os.path.join(".cc", "findings.json")
KEEP = os.path.join(".cc-cache", "verify")

# Field names say what they hold: two verifiers in three put a script's CONTENT in a field named
# `script` and their findings were unusable (run 555c4cf4).
FINDING = {"type": "object", "properties": {
    "requirement": {"type": "string", "description": "the broken requirement, quoted word for word"},
    "source": {"type": "string", "enum": ["request", "spec", "seams"]},
    "expected": {"type": "string"}, "observed": {"type": "string"},
    "file": {"type": "string", "description": "the PATH of the reproduction script you wrote, "
                                              "`.cc/checks/verify-<name>.py` (never its content)"},
    "command": {"type": "string", "description": "one argv line that runs the file from the repository root"},
    "mutation": {"type": "string", "description": "checks verifier only: a unified diff against the tree"},
    "check": {"type": "string", "description": "checks verifier only: the id of the check the mutation slips past"}},
    "required": ["requirement", "source", "expected", "observed", "file", "command"]}
VERIFY_SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string"},
    "findings": {"type": "array", "items": FINDING},
    "notes": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_NOTES}},
    "required": ["summary", "findings", "notes"]}
RULE_SCHEMA = {"type": "object", "properties": {
    "rulings": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "ruling": {"type": "string", "enum": ["fix", "followup", "reject"]},
        "reason": {"type": "string"}}, "required": ["id", "ruling", "reason"]}},
    "assessment": {"type": "string"}},
    "required": ["rulings", "assessment"]}


def plan_focuses(setting, planned) -> list[str]:
    """The verifiers of this run. ``auto``: the lead's choice; ``0``: none; ``N``: N verifiers, the lead's
    choice first, then fidelity, seams, checks. Never more than ``MAX_VERIFIERS``, never an unknown one."""
    chosen = list(dict.fromkeys(f for f in (str(x).strip().lower() for x in planned or []) if f in FOCUSES))
    if setting == "auto":
        return chosen[:MAX_VERIFIERS]
    n = min(int(setting), MAX_VERIFIERS)
    for f in DEFAULT_ORDER:
        if len(chosen) >= n:
            break
        if f not in chosen:
            chosen.append(f)
    return chosen[:n]


def verifiers_mode(value) -> str | int:
    text = str(value if value is not None else "").strip().lower()
    if text in ("", "auto"):
        return "auto"
    try:
        return max(int(float(text)), 0)
    except ValueError as e:
        raise ValueError(f"verifiers: expected auto or a number, got {value!r}") from e


def plan_guide(setting) -> str:
    if setting == 0:
        return "This run has no verification gate: return `verification.focuses` empty."
    picks = ", ".join(f"`{k}`" for k in FOCUSES)
    fixed = "" if setting == "auto" else f" The person asked for {setting} verifier(s); yours come first."
    return ("VERIFICATION. When your checks are all green and you would deliver, independent verifiers look at the "
            f"build once, each for one thing ({picks}), and the harness keeps only what it reproduces. Choose in "
            "`verification.focuses` the ones this request warrants (at most four): none for a small, well-covered "
            "change; `fidelity` and `seams` for a system of several parts; `checks` when a weak check would let a "
            "wrong build through; `security`, `data` or `performance` only when the request makes them matter."
            + fixed)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[`*_>#]", "", str(text or ""))).strip().lower()


def key(requirement: str) -> str:
    return hashlib.sha256(_norm(requirement).encode()).hexdigest()[:16]


def same_requirement(a: str, b: str) -> bool:
    """Two quotes of one requirement: equal, or one quoting part of the other (run 90247ae8 raised the same
    seam twice, once with its whole line and once with its first sentence)."""
    x, y = _norm(a), _norm(b)
    return bool(x and y) and (x in y or y in x) and min(len(x), len(y)) >= MIN_QUOTE


def quote_sources(work: str, request: str) -> dict[str, str]:
    """The texts a blocking finding may quote: the request, the spec, the seams."""
    spec = []
    for root in (".cc/openspec/specs", ".cc/spec", "design/spec"):
        for base, _, files in os.walk(os.path.join(work, root)):
            for f in sorted(files):
                if f.endswith((".md", ".tla")):
                    try:
                        spec.append(open(os.path.join(base, f), encoding="utf-8", errors="replace").read())
                    except OSError:
                        pass
    try:
        seams = open(os.path.join(work, ".cc", "design.md"), encoding="utf-8", errors="replace").read()
    except OSError:
        seams = ""
    return {"request": _norm(request), "spec": _norm("\n".join(spec)), "seams": _norm(seams)}


def quoted_in(requirement: str, sources: dict[str, str]) -> str:
    """Which source holds the quote verbatim (normalized), or "" (too short to count is "" too)."""
    q = _norm(requirement)
    if len(q) < MIN_QUOTE:
        return ""
    return next((name for name, text in sources.items() if q in text), "")


def copy_tree(src: str, dst: str, *, strip: tuple[str, ...] = ()) -> str:
    """A throwaway copy of the run's tree, dependencies included (copy-on-write where the file system can)."""
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    from common import exec as harness_exec
    cloned = False
    if platform.system() == "Darwin":
        cloned = harness_exec.run(["cp", "-c", "-R", src, dst], check=False).code == 0
    if not cloned:
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, symlinks=True)
    for rel in strip:
        path = os.path.join(dst, rel)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            os.remove(path)
    return dst


def script_problem(finding: dict, work: str) -> str:
    """Why a finding's reproduction cannot be run as a check ("" when it can)."""
    from common import architect
    raw = str(finding.get("file") or "")
    if "\n" in raw:
        return "its `file` holds a script's content, not the path of the script it wrote"
    script = os.path.normpath(raw).replace(os.sep, "/")
    if not re.fullmatch(r"\.cc/checks/verify-[A-Za-z0-9_.-]+\.(mjs|cjs|sh|py)", script):
        return f"its script `{script}` is not `.cc/checks/verify-<name>.mjs|cjs|sh|py`"
    try:
        argv = architect.check_argv(str(finding.get("command") or ""))
    except ValueError as e:
        return f"its command cannot run as a check: {e}"
    if script not in [os.path.normpath(a).replace(os.sep, "/") for a in argv]:
        return f"its command does not run its script `{script}`"
    if os.path.lexists(os.path.join(work, script)):
        return f"its script would replace `{script}`, which the run already has"
    return ""


def _run(command: str, cwd: str, env: dict) -> dict:
    from common import architect, check_execution
    r = check_execution.execute(architect.check_argv(command), cwd=cwd, env=env, stall_after_s=STALL_AFTER_S)
    return {"exitCode": r.exit_code, "stalled": bool(r.stalled), "seconds": round(r.duration_seconds or 0, 1),
            "output": ((r.stdout or "") + (r.stderr or ""))[-1500:]}


def reproduce(work: str, finding: dict, script_src: str, scratch: str, env: dict, checks: dict) -> dict:
    """Run a finding's reproduction once, in clean copies of the committed tree. ``{"ok", "why", "runs"}``:
    ``ok`` means the harness saw what the finding claims."""
    script = os.path.normpath(str(finding["file"]))
    runs = {}
    try:
        clean = copy_tree(work, os.path.join(scratch, "clean"))
        os.makedirs(os.path.dirname(os.path.join(clean, script)), exist_ok=True)
        shutil.copy2(script_src, os.path.join(clean, script))
        runs["tree"] = _run(finding["command"], clean, env)
        # Only the check-strength verifier's findings are about a check; anyone else's is a defect.
        mutation = str(finding.get("mutation") or "").strip() if finding.get("focus") == "checks" else ""
        if not mutation:
            red = runs["tree"]["exitCode"] not in (0, None) and not runs["tree"]["stalled"]
            return {"ok": red, "runs": runs,
                    "why": "" if red else ("its script stalled" if runs["tree"]["stalled"]
                                           else "its script passes on the delivered tree: nothing reproduced")}
        if runs["tree"]["exitCode"] != 0:
            return {"ok": False, "runs": runs, "why": "its stronger check fails on the delivered tree itself"}
        check = checks.get(str(finding.get("check") or ""))
        if not check:
            return {"ok": False, "runs": runs, "why": f"it names no existing check (`{finding.get('check')}`)"}
        mutant = copy_tree(work, os.path.join(scratch, "mutant"))
        os.makedirs(os.path.dirname(os.path.join(mutant, script)), exist_ok=True)
        shutil.copy2(script_src, os.path.join(mutant, script))
        from common import git
        patch = os.path.join(scratch, "mutation.diff")
        with open(patch, "w", encoding="utf-8") as fh:
            fh.write(mutation + "\n")
        applied = git.git(mutant, "apply", "--whitespace=nowarn", patch, check=False)
        if applied.code != 0:
            return {"ok": False, "runs": runs, "why": f"its mutation does not apply: {applied.stderr.strip()[:300]}"}
        runs["mutant"] = _run(finding["command"], mutant, env)
        runs["existing"] = _run(str(check.get("command") or ""), mutant, env)
        ok = runs["mutant"]["exitCode"] not in (0, None) and runs["existing"]["exitCode"] == 0
        return {"ok": ok, "runs": runs,
                "why": "" if ok else ("the existing check already fails on the mutation"
                                      if runs["existing"]["exitCode"] != 0 else "its stronger check passes on the mutation")}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def read_ledger(work: str) -> list[dict]:
    try:
        with open(os.path.join(work, LEDGER), encoding="utf-8") as fh:
            data = json.load(fh)
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def write_ledger(work: str, entries: list[dict]) -> None:
    os.makedirs(os.path.join(work, ".cc"), exist_ok=True)
    with open(os.path.join(work, LEDGER), "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=1)


def table(blocking: list[dict]) -> str:
    """What the lead reads: one entry per standing finding, the harness's evidence beside the claim."""
    out = []
    for f in blocking:
        run = (f.get("reproduction") or {}).get("runs") or {}
        evidence = "; ".join(f"{name}: exit {r.get('exitCode')}" for name, r in run.items())
        tail = (run.get("mutant") or run.get("tree") or {}).get("output") or ""
        out.append(f"### {f['id']} ({', '.join(f['focuses'])}; {'weak check' if f.get('mutation') else 'defect'})\n"
                   f"Requirement ({f['source']}): \"{f['requirement']}\"\n"
                   f"Expected: {f['expected']}\nObserved: {f['observed']}\n"
                   f"Reproduction: `{f['command']}` ({f['file']})"
                   + (f", slipping past check `{f.get('check')}`" if f.get("mutation") else "")
                   + f"\nThe harness ran it: {evidence}\n```\n{tail[-800:]}\n```")
    return "\n\n".join(out)
