"""The swarm's spec and design choices (docs/FORMAL_SPEC_DESIGN.md): optional, the person's or the
architect's; TLA+ or OpenSpec in .cc/; an existing spec is revised, never dropped; the pull request carries
only the project's own files."""
from __future__ import annotations

import json
import os

import pytest

from common import git, openspec_spec
from swarm import spec as spec_mod
from swarm.link import leave_out_harness_files
from swarm.tasks import _restore_harness_files, swarm_formal, swarm_measure

SPEC = """## Purpose
Keep a directory of users.

## Requirements
### Requirement: Create a user
The system SHALL create a user with a unique id.

#### Scenario: New id
- **WHEN** a user is created with an id not yet used
- **THEN** the user is listed

#### Scenario: Duplicate id
- **WHEN** a user is created with an id already used
- **THEN** the request is refused with a conflict
"""


def _write(root, rel, text):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _openspec(root, text=SPEC):
    _write(root, os.path.join(openspec_spec.SPECS, "users", "spec.md"), text)


def _checks(*covers):
    return [{"id": f"c{n}", "command": f"node .cc/checks/c{n}.mjs", "covers": list(c)} for n, c in enumerate(covers)]


# ─────────────────────────────── OpenSpec ───────────────────────────────
def test_an_openspec_spec_is_ready_when_well_formed_and_every_scenario_is_proven_by_a_check(tmp_path):
    _openspec(tmp_path)
    proof = openspec_spec.prove(str(tmp_path), _checks(["New id"], ["duplicate  ID"]))
    assert proof["ok"] is True and proof["scenarios"] == 2
    assert proof["coverage"] == {"New id": ["c0"], "Duplicate id": ["c1"]}  # names match without case or spacing


def test_an_openspec_spec_names_every_gap_the_architect_must_close(tmp_path):
    _openspec(tmp_path, SPEC.replace("SHALL", "will").replace("- **THEN** the request is refused with a conflict\n", ""))
    proof = openspec_spec.prove(str(tmp_path), _checks(["New id", "Missing one"]))
    assert proof["ok"] is False
    fb = proof["feedback"]
    assert "must say SHALL or MUST" in fb and "'Duplicate id': needs a `- **WHEN** ...` and a `- **THEN** ...`" in fb
    assert "covers 'Missing one', which is not a scenario" in fb and "no check proves these scenarios" in fb


def test_which_requirements_hold_follows_the_checks_that_cover_them():
    proof = {"requirements": [{"capability": "users", "name": "Create a user", "scenarios": ["New id", "Duplicate id"]}],
             "coverage": {"New id": ["c0"], "Duplicate id": ["c1"]}}
    assert "holds" in openspec_spec.status(proof, [{"id": "c0", "exitCode": 0}, {"id": "c1", "exitCode": 0}])
    held = openspec_spec.status(proof, [{"id": "c0", "exitCode": 0}, {"id": "c1", "exitCode": 1}])
    assert "NOT MET: 'Duplicate id' (check c1 not green)" in held


# ─────────────────────────────── the gate after planning ───────────────────────────────
def _plan(kind, docs=(), checks=()):
    return {"spec": {"kind": kind, "why": "because"}, "design": {"docs": list(docs), "why": "because"},
            "checks": list(checks), "retireChecks": [], "items": []}


@pytest.mark.parametrize("specs,have,kind,ok,said", [
    ("tla", "", "none", False, "asked for a TLA+ spec"),
    ("openspec", "", "tla", False, "asked for an OpenSpec spec"),
    ("auto", "tla", "none", False, "never replaced or dropped"),
    ("auto", "", "none", True, ""),
    ("none", "tla", "tla", True, ""),  # the person chose none: nothing is checked, whatever the plan says
])
def test_the_gate_holds_the_plan_to_the_runs_choice(tmp_path, specs, have, kind, ok, said):
    out = spec_mod.gate(str(tmp_path), _plan(kind), specs, "auto", have)
    assert out["ok"] is ok and said in out["feedback"]
    if ok:
        assert out["kind"] == ("none" if specs == "none" else kind) and "THE SPEC: none" in out["brief"]


def test_the_gate_checks_the_design_documents_the_choice_asks_for(tmp_path):
    assert "asked for design documents" in spec_mod.gate(str(tmp_path), _plan("none"), "auto", "always", "")["feedback"]
    out = spec_mod.gate(str(tmp_path), _plan("none", docs=["design/architecture.md"]), "auto", "auto", "")
    assert out["ok"] is False and "not in the tree: design/architecture.md" in out["feedback"]
    _write(tmp_path, "design/architecture.md", "# Architecture\n")
    out = spec_mod.gate(str(tmp_path), _plan("none", docs=["design/architecture.md"]), "auto", "auto", "")
    assert out["ok"] is True and "`design/architecture.md`" in out["brief"]
    assert spec_mod.gate(str(tmp_path), _plan("none", docs=["x.md"]), "auto", "none", "")["designDocs"] == []


def test_the_gate_proves_an_openspec_spec_against_the_plans_checks_and_the_repositorys(tmp_path, fake_task_input):
    _openspec(tmp_path)
    _write(tmp_path, spec_mod.CHECKS_FILE, json.dumps(_checks(["New id"])))  # from an earlier run
    plan = _plan("openspec", checks=[{"id": "dup", "command": "node .cc/checks/dup.mjs", "covers": ["Duplicate id"]}])
    out = swarm_formal(fake_task_input(workdir=str(tmp_path), plan={"structured": plan}, specs="auto",
                                       design="auto", existingSpec="openspec")).output_data
    assert out["ok"] is True and out["kind"] == "openspec" and out["feedback"] == ""
    plan["retireChecks"] = ["c0"]  # retiring the only check of a scenario leaves it unproven
    out = swarm_formal(fake_task_input(workdir=str(tmp_path), plan={"structured": plan}, specs="auto",
                                       design="auto", existingSpec="openspec")).output_data
    assert out["ok"] is False and "THE HARNESS REJECTED YOUR LAST PLAN" in out["feedback"] and "'New id'" in out["feedback"]


def test_existing_checks_stay_retired_ones_go_and_the_plans_replace_by_id():
    existing = _checks([], ["A"], [])
    plan = {"checks": [{"id": "c2", "command": "node new.mjs", "covers": ["B"]}], "retireChecks": ["c0"]}
    assert spec_mod.merge_checks(existing, plan) == [
        {"id": "c1", "command": "node .cc/checks/c1.mjs", "covers": ["A"]},
        {"id": "c2", "command": "node new.mjs", "covers": ["B"]}]


def test_the_architect_is_told_to_revise_an_existing_spec_and_keep_existing_checks():
    g = spec_mod.plan_guide("auto", "auto", "tla", _checks(["x"]), ["design/api.md"])
    assert "already has a TLA+ spec in `.cc/spec/`" in g["specGuide"] and "never replace or drop it" in g["specGuide"]
    assert "`c0`: `node .cc/checks/c0.mjs` (covers: x)" in g["existingChecks"] and "retireChecks" in g["existingChecks"]
    assert "`design/api.md`" in g["designGuide"] and "A small fix" in g["designGuide"]
    auto = spec_mod.plan_guide("auto", "none", "", [], [])
    assert '"none": a small fix' in auto["specGuide"] and "(tla)" in auto["specGuide"] and "(openspec)" in auto["specGuide"]
    assert "Write NO design documents" in auto["designGuide"]
    assert "Write NO spec" in spec_mod.plan_guide("none", "auto", "", [], [])["specGuide"]


def test_a_spec_in_the_old_place_moves_into_cc(tmp_path):
    repo = str(tmp_path / "r")
    git.ensure_ready(repo)
    _write(repo, "design/spec/abstraction.json", "{}")
    _write(repo, "design/spec/M.tla", "---- MODULE M ----\n====\n")
    git.git(repo, "add", "-A")
    git.git(repo, "commit", "-qm", "old layout")
    assert spec_mod.migrate_legacy(repo) is True
    assert os.path.isfile(os.path.join(repo, ".cc", "spec", "M.tla")) and not os.path.exists(os.path.join(repo, "design", "spec"))
    assert spec_mod.existing_kind(repo) == "tla" and spec_mod.migrate_legacy(repo) is False


# ─────────────────────────────── measuring ───────────────────────────────
def test_a_spec_removed_after_planning_is_a_red_check_not_a_check_that_vanishes(tmp_path, fake_task_input):
    repo = str(tmp_path / "clone")
    git.ensure_ready(repo)
    _write(repo, spec_mod.CHECKS_FILE, json.dumps([{"id": "ok", "command": "node -e 0"}]))
    out = swarm_measure(fake_task_input(workdir=repo, label="r1", specKind="tla")).output_data
    proof = next(r for r in out["results"] if r["id"] == "spec-proof")
    assert out["allGreen"] is False and proof["exitCode"] == 1 and "gone from .cc/spec/" in proof["output"]
    out = swarm_measure(fake_task_input(workdir=repo, label="r1", specKind="none")).output_data
    assert out["allGreen"] is True and [r["id"] for r in out["results"]] == ["ok"]


def test_an_openspec_measurement_says_which_requirements_hold(tmp_path, fake_task_input):
    repo = str(tmp_path / "clone")
    git.ensure_ready(repo)
    _openspec(repo)
    _write(repo, spec_mod.CHECKS_FILE, json.dumps([{"id": "new", "command": "node -e 0", "covers": ["New id"]},
                                                   {"id": "dup", "command": "node -e process.exit(1)",
                                                    "covers": ["Duplicate id"]}]))
    out = swarm_measure(fake_task_input(workdir=repo, label="r1", specKind="openspec")).output_data
    coverage = next(r for r in out["results"] if r["id"] == "spec-coverage")
    assert coverage["exitCode"] == 0 and "NOT MET: 'Duplicate id' (check dup not green)" in coverage["output"]
    assert out["allGreen"] is False  # the failing check itself is red


# ─────────────────────────────── publishing ───────────────────────────────
def _history(tmp_path):
    repo = str(tmp_path / "repo")
    git.ensure_ready(repo)
    _write(repo, "src/app.py", "print(1)\n")
    _write(repo, ".cc/checks.json", "[]\n")  # the project already had one harness file
    git.git(repo, "add", "-A")
    git.git(repo, "commit", "-qm", "base")
    base = git.head(repo)
    git.git(repo, "switch", "-qc", "feature")
    _write(repo, "src/app.py", "print(2)\n")
    _write(repo, ".cc/checks.json", '[{"id": "x"}]\n')
    _write(repo, ".cc/spec/M.tla", "spec\n")
    _write(repo, ".conductor-code/repo_memory.json", "{}\n")
    git.git(repo, "add", "-A")
    git.git(repo, "commit", "-qm", "round 1")
    return repo, base, git.head(repo)


def test_the_pull_request_carries_only_the_projects_own_changes(tmp_path):
    repo, base, head = _history(tmp_path)
    commit, ref = leave_out_harness_files(repo, branch="feature", head=head, base=base, run_id="abcdef123456",
                                          message="Title\n\nSummary")
    assert git.git(repo, "rev-parse", f"{commit}^").stdout.strip() == base
    assert git.git(repo, "diff", "--name-only", base, commit).stdout.split() == ["src/app.py"]
    assert git.git(repo, "show", f"{commit}:.cc/checks.json").stdout == "[]\n"  # as the base had it
    assert ref == "refs/conductor/abcdef12/harness" and git.git(repo, "rev-parse", ref).stdout.strip() == head
    assert git.head(repo) == commit  # the branch is what was published
    again, _ = leave_out_harness_files(repo, branch="feature", head=head, base=base, run_id="abcdef123456",
                                       message="Title\n\nSummary")
    assert again == commit  # a retried publish pushes the same commit


def test_a_follow_up_brings_back_the_harness_files_the_pull_request_left_out(tmp_path):
    repo, base, head = _history(tmp_path)
    leave_out_harness_files(repo, branch="feature", head=head, base=base, run_id="abcdef123456", message="m")
    work = str(tmp_path / "work")
    git.git(repo, "clone", "-q", "--no-local", "--branch", "feature", repo, work)
    assert not os.path.exists(os.path.join(work, ".cc", "spec"))
    _restore_harness_files(work, repo, "refs/conductor/abcdef12/harness")
    assert open(os.path.join(work, ".cc", "spec", "M.tla")).read() == "spec\n"
    assert os.path.isfile(os.path.join(work, ".conductor-code", "repo_memory.json"))
