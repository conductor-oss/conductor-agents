"""The verification gate's guards against noise: only what the harness reproduces and what quotes the
request, spec or seams can block; one finding per requirement; nothing ruled is raised again; the lead
rules, a fix becomes a frozen check, and the gate runs once (swarm/verify.py)."""
from __future__ import annotations

import json
import os
import sys

from common import git
from swarm import verify
from swarm.tasks import GATE_KIND, swarm_gate, swarm_gate_apply, swarm_round, swarm_verify_fork

REQUEST = "Add an adder: add(a, b) must return the sum of a and b, and add must reject non-numbers."
PY = sys.executable


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def _work(tmp_path):
    """A delivered tree: a correct adder, a weak check (only 0+0), and the seams."""
    work = str(tmp_path / "repo" / ".cc-worktrees" / "swarm-run")
    git.ensure_ready(work)
    _write(work, "app.py", "def add(a, b):\n    return a + b\n")
    _write(work, ".cc/design.md", "# Seams\nThe adder lives in app.py as add(a, b).\n")
    _write(work, ".cc/checks/add.py", "import sys; sys.path.insert(0, '.'); from app import add; sys.exit(0 if add(0, 0) == 0 else 1)\n")
    _write(work, ".cc/checks.json", json.dumps([{"id": "add", "file": ".cc/checks/add.py",
                                                 "command": f"{PY} .cc/checks/add.py", "scenario": "0+0", "covers": []}]))
    git.git(work, "add", "-A")
    git.git(work, "commit", "-q", "-m", "delivered")
    return work


def _finding(copy, name, requirement, code, **extra):
    script = f".cc/checks/verify-{name}.py"
    _write(copy, script, code)
    return {"requirement": requirement, "source": "request", "expected": "e", "observed": "o",
            "file": script, "command": f"{PY} {script}", **extra}


FAILS = "import sys; sys.path.insert(0, '.'); from app import add\ntry:\n    add('x', 'y')\nexcept TypeError:\n    sys.exit(0)\nsys.exit(1)\n"
PASSES = "import sys; sys.exit(0)\n"
STRONG = "import sys; sys.path.insert(0, '.'); from app import add; sys.exit(0 if add(2, 3) == 5 else 1)\n"
MUTATION = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a * b
"""


def test_which_verifiers_run():
    assert verify.plan_focuses("auto", ["seams", "bogus", "seams"]) == ["seams"]
    assert verify.plan_focuses("auto", []) == [] and verify.plan_focuses(0, ["fidelity"]) == []
    assert verify.plan_focuses(3, ["security"]) == ["security", "fidelity", "seams"]
    assert len(verify.plan_focuses(9, list(verify.FOCUSES))) == verify.MAX_VERIFIERS


def test_overlapping_quotes_are_one_requirement():
    assert verify.same_requirement("POST /api/users -> 201 created user",
                                   "POST /api/users -> `201` created user. Required: a nonblank id")
    assert verify.same_requirement("add must reject", "add must reject non-numbers and floats")
    assert not verify.same_requirement("add", "add must reject non-numbers")  # too short to count
    assert not verify.same_requirement("list prints items", "remove deletes an item")


def test_a_quote_must_be_real_and_long_enough():
    sources = {"request": verify._norm(REQUEST), "spec": "", "seams": verify._norm("The `adder` lives in app.py")}
    assert verify.quoted_in("add must  reject non-numbers", sources) == "request"
    assert verify.quoted_in("the adder lives in app.py", sources) == "seams"
    assert verify.quoted_in("add", sources) == ""  # too short to quote anything
    assert verify.quoted_in("add must be fast", sources) == ""


def _gate(tmp_path, fake_task_input, monkeypatch):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    work = _work(tmp_path)
    run = "run-gate-1"
    verify.write_ledger(work, [{"key": verify.key("the adder lives in app.py as add(a, b)"), "ruling": "reject",
                                "reason": "settled last run"}])
    git.git(work, "add", "-A")
    git.git(work, "commit", "-q", "-m", "an earlier run's ruling")
    fid = verify.copy_tree(work, str(tmp_path / "copy-fidelity"))
    chk = verify.copy_tree(work, str(tmp_path / "copy-checks"))
    flood = verify.copy_tree(work, str(tmp_path / "copy-flood"))
    reject = "add must reject non-numbers"
    findings_fid = [
        # reproduces: stands; a stray mutation from a verifier other than `checks` does not make it a check finding
        _finding(fid, "reject", reject, FAILS, mutation=MUTATION, check="add"),
        _finding(fid, "sum", "add(a, b) must return the sum of a and b", PASSES),  # passes: not reproduced
        _finding(fid, "fast", "add must be fast enough for a billion calls", FAILS),  # quotes nothing: follow-up
        _finding(fid, "seam", "The adder lives in app.py as add(a, b)", FAILS),    # already ruled
        {**_finding(fid, "badpath", reject, FAILS), "file": "tests/x.py"},       # unusable reproduction
    ]
    findings_chk = [
        _finding(chk, "reject2", "add must reject non-numbers.", FAILS),  # the same requirement quoted again: merged
        {**_finding(chk, "content", "add(a, b) must return the sum of a and b", FAILS), "file": FAILS},  # content, not a path
        _finding(chk, "strong", "add(a, b) must return the sum of a and b", STRONG, mutation=MUTATION, check="add"),
    ]
    findings_flood = [_finding(flood, f"n{n}", f"flood finding number {n} here", PASSES) for n in range(7)]
    fork = {"copies": {"verify_fidelity": {"focus": "fidelity", "path": fid}, "verify_checks": {"focus": "checks", "path": chk},
                       "verify_data": {"focus": "data", "path": flood}, "verify_seams": {"focus": "seams", "path": str(tmp_path / "gone")}},
            "focuses": ["fidelity", "checks", "data", "seams"]}
    # As the join reports them inside the round loop: `<ref>__<iteration>`.
    results = {"verify_fidelity__1": {"structured": {"summary": "s", "findings": findings_fid, "notes": ["n1", "n2", "n3", "n4"]}},
               "verify_checks__1": {"structured": {"summary": "s", "findings": findings_chk, "notes": []}},
               "verify_data__1": {"structured": {"summary": "s", "findings": findings_flood, "notes": []}},
               "verify_seams__1": {"status": "failed"}}
    out = swarm_gate(fake_task_input(workdir=work, runId=run, description=REQUEST, fork=fork, results=results)).output_data
    return work, run, fork, out


def test_only_reproduced_quoted_new_findings_block(tmp_path, fake_task_input, monkeypatch):
    work, run, fork, out = _gate(tmp_path, fake_task_input, monkeypatch)
    blocking = {b["requirement"]: b for b in out["blocking"]}
    assert set(blocking) == {"add must reject non-numbers", "add(a, b) must return the sum of a and b"}
    merged = blocking["add must reject non-numbers"]
    assert merged["focuses"] == ["checks", "fidelity"] and merged["reproduction"]["runs"]["tree"]["exitCode"] == 1
    weak = blocking["add(a, b) must return the sum of a and b"]
    runs = weak["reproduction"]["runs"]
    assert runs["tree"]["exitCode"] == 0 and runs["mutant"]["exitCode"] != 0 and runs["existing"]["exitCode"] == 0
    assert [f["requirement"] for f in out["followUps"]] == ["add must be fast enough for a billion calls"]
    why = " | ".join(d["why"] for d in out["dropped"])
    assert "not reproduced" in why and "already ruled (reject)" in why and "unusable reproduction" in why
    assert "holds a script's content" in why
    assert "over 5 findings from one verifier" in why
    assert merged["mutation"] == ""  # the fidelity verifier's stray mutation does not make it a check finding
    assert len([n for n in out["notes"] if n.startswith("[fidelity]")]) == verify.MAX_NOTES
    assert out["timedOut"] == ["seams"] and out["hasBlocking"] == "rule"
    assert "The harness ran it" in out["table"] and "weak check" in out["table"]
    assert git.git(work, "status", "--porcelain").stdout.strip() == ""  # reproductions never touch the run's tree


def test_the_lead_rules_fixes_become_checks_and_the_run_reopens_once(tmp_path, fake_task_input, monkeypatch):
    work, run, fork, out = _gate(tmp_path, fake_task_input, monkeypatch)
    ids = {b["requirement"]: b["id"] for b in out["blocking"]}
    rule = {"structured": {"assessment": "two real gaps", "rulings": [
        {"id": ids["add must reject non-numbers"], "ruling": "fix", "reason": "the request says so"},
        {"id": ids["add(a, b) must return the sum of a and b"], "ruling": "fix", "reason": "weak check"}],
        "items": [{"id": "reject-input", "title": "t", "brief": "b", "kind": "api", "rung": 0, "dependsOn": []}]}}
    applied = swarm_gate_apply(fake_task_input(workdir=work, runId=run, repoPath=work, gate=out, rule=rule, fork=fork,
                                               assessment="delivered", ceilingUsd=0)).output_data
    assert applied["reopen"] is True and applied["delivered"] is False and applied["items"][0]["id"] == "reject-input"
    checks = json.load(open(os.path.join(work, ".cc/checks.json")))
    assert len(checks) == 3 and all(os.path.isfile(os.path.join(work, c["file"])) for c in checks)
    ledger = {e.get("requirement"): e["ruling"] for e in verify.read_ledger(work)}
    assert ledger["add must reject non-numbers"] == "fix" and ledger["add must be fast enough for a billion calls"] == "followup"
    assert "verification" in git.git(work, "log", "-1", "--format=%s").stdout
    assert not os.path.exists(str(tmp_path / "copy-fidelity"))  # the copies are gone
    assert "Follow-ups" in applied["assessment"] and applied["verification"]["adoptedChecks"]
    from common import blackboard as bb
    from common import rung_policy
    verdicts = [f["data"] for f in bb.read_facts(rung_policy.OUTCOME_RUN, kind=rung_policy.OUTCOME_KIND)
                if f["data"].get("role") == "verify"]
    assert {(v["kind"], v["ok"]) for v in verdicts} >= {("fidelity", True), ("checks", True)}


def test_no_ruling_is_a_follow_up_and_a_weak_check_alone_does_not_reopen(tmp_path, fake_task_input, monkeypatch):
    work, run, fork, out = _gate(tmp_path, fake_task_input, monkeypatch)
    ids = {b["requirement"]: b["id"] for b in out["blocking"]}
    rule = {"structured": {"assessment": "", "items": [], "rulings": [
        {"id": ids["add(a, b) must return the sum of a and b"], "ruling": "fix", "reason": "weak check"}]}}
    applied = swarm_gate_apply(fake_task_input(workdir=work, runId=run, repoPath=work, gate=out, rule=rule, fork=fork,
                                               assessment="", ceilingUsd=0)).output_data
    assert applied["reopen"] is False and applied["delivered"] is True
    assert any(f["why"] == "the lead gave no ruling" for f in applied["followUps"])


def test_the_gate_runs_once_and_only_on_a_green_delivery(tmp_path, fake_task_input, monkeypatch):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    work = _work(tmp_path)
    run = "run-once"

    def decide(**kw):
        base = dict(runId=run, round=1, repoPath=work, allGreen=True, ceilingUsd=0, verifiers="auto",
                    verification={"focuses": ["fidelity", "seams"], "why": "parts"},
                    evaluation={"structured": {"delivered": True, "assessment": "done", "items": []}})
        return swarm_round(fake_task_input(**{**base, **kw})).output_data

    assert decide()["gate"] == "verify" and decide()["focuses"] == ["fidelity", "seams"]
    assert decide(allGreen=False)["gate"] == "skip"
    assert decide(verifiers="0")["gate"] == "skip"
    assert decide(verification={"focuses": [], "why": "small"})["gate"] == "skip"
    fork = swarm_verify_fork(fake_task_input(workdir=work, runId=run, round=1, description=REQUEST,
                                             focuses=["fidelity", "seams"], measurement="all green")).output_data
    assert decide()["gate"] == "skip"  # never twice
    from common import blackboard as bb
    assert bb.read_facts(run, kind=GATE_KIND)
    blind, seams = fork["copies"]["verify_fidelity"]["path"], fork["copies"]["verify_seams"]["path"]
    assert not os.path.exists(os.path.join(blind, ".cc", "design.md")) and not os.path.exists(os.path.join(blind, ".git"))
    assert os.path.isfile(os.path.join(seams, ".cc", "design.md"))
    inp = fork["dynamicTasksInput"]["verify_fidelity"]
    assert inp["modelRole"] == "judge" and "feedbackRunId" not in inp and inp["sharedTree"] is False
    from common import prompt_files
    _, ctx = prompt_files.load(inp["promptPath"])
    assert ctx["measurement"] == "" and ctx["focus"] == "fidelity"
    _, seams_ctx = prompt_files.load(fork["dynamicTasksInput"]["verify_seams"]["promptPath"])
    assert "all green" in seams_ctx["measurement"]


def test_the_workflow_wires_the_gate_once_per_run():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    w = json.load(open(os.path.join(here, "workflows", "swarm.json")))
    assert w["inputTemplate"]["architects"] == "auto" and w["inputTemplate"]["verifiers"] == "auto"
    rounds = next(t for t in w["tasks"] if t["taskReferenceName"] == "rounds")
    sw = next(t for t in rounds["loopOver"] if t["taskReferenceName"] == "gate_switch")
    assert [t["taskReferenceName"] for t in sw["decisionCases"]["verify"]] == [
        "verify_fork", "verifiers", "verifiers_join", "gate", "rule_switch", "gate_apply", "next_gate"]
    assert [t["taskReferenceName"] for t in sw["defaultCase"]] == ["next"]
    assert "gate_apply" in rounds["loopCondition"] and "iteration'] <" in rounds["loopCondition"]
    item = json.load(open(os.path.join(here, "workflows", "swarm_item.json")))
    work = next(t for t in item["tasks"] if t["taskReferenceName"] == "work")
    assert work["inputParameters"]["modelRole"] == "${prep.output.modelRole}"
    assert work["inputParameters"]["allowedWriteRoots"] == "${prep.output.writeRoots}"
