"""Pure-logic invariant tests over the workflow / taskdef JSON.

These encode, as pytest, the registration invariants that ``workers/register.sh``
only checks by hand at deploy time — so CI catches a bad JSON edit (unmapped SIMPLE
task, dangling sub-workflow reference, inconsistent version pin, malformed JSON)
*before* anyone runs ``register.sh``. Modeled on security-harness's
``tests/test_workflow_jq.py`` / ``tests/test_wiring_cores.py``.

Contract (see CONTEXT.md): pure-logic — no live Conductor server, no network, no
real LLM. Everything here is ``json.load`` + string inspection over the tree.

Invariants covered:
  1. Every workflow / taskdef JSON is well-formed (mirrors ``make validate``).
  2. SIMPLE-task coverage: every ``"type": "SIMPLE"`` task in a workflow has a
     matching taskdef file by ``name`` (mirrors register.sh's jq logic in Python).
  3. Sub-workflow references resolve to a workflow in the tree, and any version pin
     is consistent with that workflow file's ``version``.
  4. Task-name uniqueness across taskdef files.
  5. (Parity) every SIMPLE task / taskdef maps to a registered ``@worker_task``.
  6. Design-loop wiring (PR #5): the DO_WHILE / SWITCH gate in the four reshaped
     workflows is internally consistent — every ``${ref.output...}`` interpolation
     resolves to a task reference in the same workflow, no SWITCH branch dangles,
     and design_docs's iterative review loop has the expected shape.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import WORKERS

# --- locate the JSON tree ---------------------------------------------------
WORKFLOWS_DIR = WORKERS / "workflows"
TASKDEFS_DIR = WORKFLOWS_DIR / "taskdefs"

# Non-recursive: taskdefs/ is a subdir, so *.json here is workflows only.
WORKFLOW_FILES = sorted(WORKFLOWS_DIR.glob("*.json"))
TASKDEF_FILES = sorted(TASKDEFS_DIR.glob("*.json"))

# The four workflows PR #5 reshaped with the iterative design-review gate.
RESHAPED = ("code_parallel", "address_pr", "issue_to_pr", "design_docs")

# ``${<token>...}`` interpolation: capture the leading reference identifier.
INTERP = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
# ``${<token>...}`` where <token> is anything but these is a task reference.
NON_TASK_TOKENS = {"workflow"}


# --- helpers ----------------------------------------------------------------

def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _iter_objects(node):
    """Yield every dict in the tree (mirrors jq's ``.. | objects``)."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_objects(v)


# Keys under a task that hold nested inline tasks (branches / loop bodies).
def _child_task_lists(task: dict):
    for case in (task.get("decisionCases") or {}).values():
        yield case
    if task.get("defaultCase"):
        yield task["defaultCase"]
    if task.get("loopOver"):
        yield task["loopOver"]
    for group in task.get("forkTasks") or []:
        # forkTasks is a list of lists of tasks.
        yield group


def _collect_tasks(wf: dict) -> list[dict]:
    """All task nodes, descending into SWITCH/DO_WHILE/FORK branches."""
    out: list[dict] = []

    def walk(tasks):
        for t in tasks:
            if not isinstance(t, dict):
                continue
            out.append(t)
            for child in _child_task_lists(t):
                walk(child)

    walk(wf.get("tasks", []))
    return out


def _task_refs(wf: dict) -> set[str]:
    return {t["taskReferenceName"] for t in _collect_tasks(wf) if "taskReferenceName" in t}


def _iter_strings(node):
    """Every string value anywhere in the structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _iter_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_strings(v)


def _referenced_tokens(wf: dict) -> set[str]:
    """Leading identifiers of every ``${...}`` interpolation in the workflow."""
    tokens: set[str] = set()
    for s in _iter_strings(wf):
        tokens.update(INTERP.findall(s))
    return tokens


def _taskdef_names() -> list[str]:
    return [_load(f)["name"] for f in TASKDEF_FILES]


def _worker_task_names() -> set[str]:
    """``task_definition_name=`` values from the harness's ``@worker_task``s."""
    pat = re.compile(r'@worker_task\(\s*task_definition_name\s*=\s*"([^"]+)"')
    names: set[str] = set()
    for mod in ("coding_agent/tasks.py", "gitops/tasks.py", "checks/tasks.py"):
        names.update(pat.findall((WORKERS / mod).read_text()))
    return names


# --- sanity: the tree we expect is actually present -------------------------

def test_tree_is_present():
    assert WORKFLOW_FILES, "no workflow JSON found"
    assert TASKDEF_FILES, "no taskdef JSON found"


# --- 1. well-formed JSON ----------------------------------------------------

@pytest.mark.parametrize(
    "path",
    WORKFLOW_FILES + TASKDEF_FILES,
    ids=lambda p: str(p.relative_to(WORKFLOWS_DIR)),
)
def test_json_is_well_formed(path: Path):
    obj = _load(path)
    assert isinstance(obj, dict)
    assert obj.get("name"), f"{path.name} has no name"
    # Workflow file basename must match its declared name (register.sh globs by
    # filename but registers by the JSON's name; a mismatch breaks ordering).
    if path.parent == WORKFLOWS_DIR:
        assert obj["name"] == path.stem, f"{path.name}: name != filename"


# --- 2. SIMPLE-task coverage (mirror register.sh) ---------------------------

def _simple_task_names() -> set[str]:
    """Mirror ``jq '.. | objects | select(.type=="SIMPLE") | .name'``."""
    names: set[str] = set()
    for f in WORKFLOW_FILES:
        for obj in _iter_objects(_load(f)):
            if obj.get("type") == "SIMPLE" and "name" in obj:
                names.add(obj["name"])
    return names


def test_every_simple_task_has_a_taskdef():
    simple = _simple_task_names()
    assert simple, "expected at least one SIMPLE task in the workflows"
    have = set(_taskdef_names())
    missing = sorted(simple - have)
    assert not missing, f"SIMPLE tasks with no local taskdef: {missing}"


# --- 3. sub-workflow references resolve + version pins consistent -----------

def test_sub_workflow_references_resolve():
    by_name = {_load(f)["name"]: _load(f) for f in WORKFLOW_FILES}
    seen = 0
    for f in WORKFLOW_FILES:
        for obj in _iter_objects(_load(f)):
            if obj.get("type") != "SUB_WORKFLOW":
                continue
            seen += 1
            param = obj.get("subWorkflowParam") or {}
            target = param.get("name")
            assert target in by_name, (
                f"{f.name}: SUB_WORKFLOW references unknown workflow {target!r}"
            )
            if "version" in param:
                assert param["version"] == by_name[target]["version"], (
                    f"{f.name}: SUB_WORKFLOW {target!r} pinned v{param['version']} "
                    f"but that workflow is v{by_name[target]['version']}"
                )
    assert seen, "expected at least one static SUB_WORKFLOW reference"


# --- 4. task-name uniqueness across taskdefs --------------------------------

def test_taskdef_names_unique():
    names = _taskdef_names()
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate taskdef names: {dupes}"


# --- 5. parity: SIMPLE tasks / taskdefs map to a real @worker_task ----------

def test_simple_tasks_map_to_registered_worker_tasks():
    workers = _worker_task_names()
    assert workers, "found no @worker_task decorators to check against"
    missing = sorted(_simple_task_names() - workers)
    assert not missing, f"SIMPLE tasks with no @worker_task: {missing}"


def test_taskdefs_map_to_registered_worker_tasks():
    workers = _worker_task_names()
    missing = sorted(set(_taskdef_names()) - workers)
    assert not missing, f"taskdefs with no @worker_task: {missing}"


# --- 6. wiring: every interpolation resolves + no dangling branch -----------

@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.stem)
def test_task_reference_names_unique_within_workflow(path: Path):
    tasks = _collect_tasks(_load(path))
    refs = [t["taskReferenceName"] for t in tasks if "taskReferenceName" in t]
    dupes = sorted({r for r in refs if refs.count(r) > 1})
    assert not dupes, f"{path.name}: duplicate taskReferenceName {dupes}"


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.stem)
def test_all_output_references_resolve(path: Path):
    """Every ``${ref...}`` (other than ``${workflow...}``) points at a task ref
    that exists somewhere in this workflow — including inside SWITCH/DO_WHILE
    branches. A dangling reference here is exactly what a bad edit introduces."""
    wf = _load(path)
    refs = _task_refs(wf)
    dangling = sorted(
        tok for tok in _referenced_tokens(wf)
        if tok not in NON_TASK_TOKENS and tok not in refs
    )
    assert not dangling, f"{path.name}: references to unknown task refs: {dangling}"


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.stem)
def test_no_dangling_switch_branches(path: Path):
    """Each SWITCH decision case is a non-empty list of well-formed tasks, and
    every DO_WHILE has a non-empty body. (An empty ``defaultCase`` is allowed —
    it is the standard 'do nothing else' escape hatch.)"""
    for t in _collect_tasks(_load(path)):
        ttype = t.get("type")
        if ttype in ("SWITCH", "DECISION"):
            cases = t.get("decisionCases") or {}
            assert cases, f"{path.name}: {t.get('taskReferenceName')} SWITCH has no cases"
            for case_name, branch in cases.items():
                assert branch, (
                    f"{path.name}: {t.get('taskReferenceName')} case {case_name!r} is empty"
                )
                for child in branch:
                    for key in ("name", "taskReferenceName", "type"):
                        assert child.get(key), (
                            f"{path.name}: task in {case_name!r} missing {key}"
                        )
        elif ttype == "DO_WHILE":
            assert t.get("loopOver"), (
                f"{path.name}: {t.get('taskReferenceName')} DO_WHILE has empty loopOver"
            )


def test_reshaped_workflows_present():
    stems = {p.stem for p in WORKFLOW_FILES}
    assert set(RESHAPED) <= stems, f"missing reshaped workflows: {set(RESHAPED) - stems}"


@pytest.mark.parametrize("name", RESHAPED)
def test_reshaped_gate_references_resolve(name: str):
    """Focused restatement of the resolution check for the four PR #5 workflows:
    the design-review gate (and everything it feeds) references only tasks that
    exist in the same workflow."""
    wf = _load(WORKFLOWS_DIR / f"{name}.json")
    refs = _task_refs(wf)
    dangling = sorted(
        tok for tok in _referenced_tokens(wf)
        if tok not in NON_TASK_TOKENS and tok not in refs
    )
    assert not dangling, f"{name}: gate references unknown task refs: {dangling}"


def test_design_docs_loop_wiring():
    """design_docs is the heart of the PR #5 design-review gate; assert its
    iterative DO_WHILE / SWITCH structure is intact and internally consistent."""
    wf = _load(WORKFLOWS_DIR / "design_docs.json")
    tasks = _collect_tasks(wf)
    by_ref = {t["taskReferenceName"]: t for t in tasks if "taskReferenceName" in t}

    # The loop itself.
    loop = by_ref.get("design_loop")
    assert loop and loop["type"] == "DO_WHILE", "design_loop DO_WHILE missing"
    body_refs = {t["taskReferenceName"] for t in loop["loopOver"]}
    assert {"design", "capture_design_result", "review_mode"} <= body_refs

    # The in-loop human-vs-judge review switch, both branches wired.
    review_mode = by_ref.get("review_mode")
    assert review_mode and review_mode["type"] == "SWITCH"
    human_branch = {t["taskReferenceName"] for t in review_mode["decisionCases"]["true"]}
    assert {"design_review", "set_human_review"} <= human_branch
    assert by_ref["design_review"]["type"] == "HUMAN"
    judge_branch = {t["taskReferenceName"] for t in review_mode["defaultCase"]}
    assert {"design_judge", "set_judge_review"} <= judge_branch

    # The post-loop approval gate: approve -> commit, else -> fail closed.
    approval = by_ref.get("approval_result")
    assert approval and approval["type"] == "SWITCH"
    assert by_ref["commit_design"]["taskReferenceName"] in {
        t["taskReferenceName"] for t in approval["decisionCases"]["true"]
    }
    assert any(t["type"] == "TERMINATE" for t in approval["defaultCase"]), (
        "design_docs must fail closed when no design is approved"
    )

    # Every SET_VARIABLE writes only variables declared on the workflow.
    declared = set(wf.get("variables", {}))
    assert declared, "design_docs should declare loop variables"
    for t in tasks:
        if t.get("type") == "SET_VARIABLE":
            unknown = set(t.get("inputParameters", {})) - declared
            assert not unknown, (
                f"{t['taskReferenceName']} sets undeclared variables: {sorted(unknown)}"
            )


# --- 7. run_checks gate wiring (CCOR-13227) ---------------------------------

def test_code_subtask_gates_commit_behind_run_checks():
    """commit runs only inside verify_gate's true case, downstream of run_checks."""
    wf = _load(WORKFLOWS_DIR / "code_subtask.json")
    by_ref = {t["taskReferenceName"]: t for t in _collect_tasks(wf) if "taskReferenceName" in t}
    assert by_ref["check"]["name"] == "run_checks"
    gate = by_ref["verify_gate"]
    assert gate["type"] == "SWITCH"
    true_case = {t["taskReferenceName"] for t in gate["decisionCases"]["true"]}
    assert "cmt" in true_case                       # commit is gated
    assert gate["decisionCases"]["true"][0]["name"] == "commit"
    # commit must NOT also exist as a top-level (ungated) task.
    top = {t["taskReferenceName"] for t in wf["tasks"]}
    assert "cmt" not in top


def test_code_parallel_verifies_after_merge_before_aggregate():
    """run_checks runs after the merge and before the aggregate, and its result
    is surfaced on the workflow output."""
    wf = _load(WORKFLOWS_DIR / "code_parallel.json")
    order = [t["taskReferenceName"] for t in wf["tasks"]]
    assert order.index("merge") < order.index("verify") < order.index("aggregate")
    verify = next(t for t in wf["tasks"] if t["taskReferenceName"] == "verify")
    assert verify["name"] == "run_checks"
    assert wf["outputParameters"]["verified"] == "null"


def test_run_checks_only_in_code_workflows():
    """run_checks must not leak into the unrelated workflows."""
    for name in ("design_docs", "pr_review", "address_pr", "issue_to_pr", "github_demo"):
        names = {o.get("name") for o in _iter_objects(_load(WORKFLOWS_DIR / f"{name}.json"))}
        assert "run_checks" not in names


# --- 7. run_checks gate wiring (see docs/design/testing.md §2) ---------------

def test_code_subtask_gates_commit_behind_run_checks():
    """commit runs only inside verify_gate's true case, downstream of run_checks."""
    wf = _load(WORKFLOWS_DIR / "code_subtask.json")
    by_ref = {t["taskReferenceName"]: t for t in _collect_tasks(wf) if "taskReferenceName" in t}
    assert by_ref["check"]["name"] == "run_checks"
    gate = by_ref["verify_gate"]
    assert gate["type"] == "SWITCH"
    true_case = {t["taskReferenceName"] for t in gate["decisionCases"]["true"]}
    assert "cmt" in true_case                       # commit is gated
    assert gate["decisionCases"]["true"][0]["name"] == "commit"
    # commit must NOT also exist as a top-level (ungated) task.
    top = {t["taskReferenceName"] for t in wf["tasks"]}
    assert "cmt" not in top


def test_code_parallel_verifies_after_merge_before_aggregate():
    wf = _load(WORKFLOWS_DIR / "code_parallel.json")
    order = [t["taskReferenceName"] for t in wf["tasks"]]
    assert order.index("merge") < order.index("verify") < order.index("aggregate")
    verify = next(t for t in wf["tasks"] if t["taskReferenceName"] == "verify")
    assert verify["name"] == "run_checks"
    assert wf["outputParameters"]["verified"] == "null"


def test_run_checks_only_in_code_workflows():
    """run_checks must not leak into the unrelated workflows."""
    for name in ("design_docs", "pr_review", "address_pr", "issue_to_pr", "github_demo"):
        names = {o.get("name") for o in _iter_objects(_load(WORKFLOWS_DIR / f"{name}.json"))}
        assert "run_checks" not in names
