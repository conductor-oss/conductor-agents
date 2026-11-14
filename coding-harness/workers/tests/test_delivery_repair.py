from __future__ import annotations

import json
import os
from pathlib import Path

from gitops.tasks import delivery_audit


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


def _load(name: str) -> dict:
    return json.loads((WORKFLOWS / f"{name}.json").read_text())


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _task(workflow: dict, ref: str) -> dict:
    return next(node for node in _walk(workflow)
                if node.get("taskReferenceName") == ref)


def test_delivery_audit_uses_exact_paths_and_structured_failures(
    tmp_git_repo, fake_task_input
):
    (tmp_git_repo / "src.py").write_text("print('changed')\n")
    report = {
        "status": "success",
        "agentCompleted": True,
        "structured": {"note": "diagnostic only"},
        "denials": ["sandbox denied image encoder"],
        "turns": [{"commands": [
            {"command": "make asset", "exitCode": 2, "stderr": "missing tool"},
        ]}],
    }

    result = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo),
        plannedPaths=["src.py", "assets/result.png"],
        agentReport=report,
    ))

    out = result.output_data
    assert out["state"] == "incomplete_delivery"
    assert out["actualPaths"] == ["src.py"]
    assert out["missingPaths"] == ["assets/result.png"]
    assert out["sandboxDenials"] == ["sandbox denied image encoder"]
    assert out["commandFailures"] == [{
        "command": "make asset", "exitCode": 2, "stderr": "missing tool",
    }]
    assert out["previousReport"] == {"note": "diagnostic only"}


def test_delivery_audit_never_requires_an_optional_path(tmp_git_repo, fake_task_input):
    """Every item with a frozen acceptance test was audited incomplete_delivery because the test —
    writable only so a WRONG assertion can be amended — was a planned path the implementer rightly
    never touched: three no-op repair agents per fork, 15 of 16 forks (run a33d7061)."""
    (tmp_git_repo / "src.py").write_text("print('changed')\n")
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["src.py", "tests/acceptance/test_accept_x.py"],
        optionalPaths=["tests/acceptance/test_accept_x.py"], agentReport={})).output_data
    assert out["state"] == "passed" and out["missingPaths"] == []
    assert out["optionalPaths"] == ["tests/acceptance/test_accept_x.py"]
    from common import code_subtask as code_subtask_logic
    result = code_subtask_logic.resolve_delivery_outcome(
        planned_paths=["src.py", "tests/acceptance/test_accept_x.py"],
        optional_paths=["tests/acceptance/test_accept_x.py"],
        commit={"stagedPaths": ["src.py"], "rejectedPaths": [], "commit": "abc", "noOp": False},
        audit={}, agent={}, tested=None, test_state=None, tests_passed=None)
    assert result["passed"] is True and result["missingPaths"] == []
    assert result["optionalPaths"] == ["tests/acceptance/test_accept_x.py"]


def test_code_subtask_hands_its_acceptance_tests_to_every_audit_as_optional():
    workflow = _load("code_subtask")
    assert "acceptanceTests" in workflow["inputParameters"]
    assert workflow["inputTemplate"]["acceptanceTests"] == []
    for ref in ("initial_delivery_audit", "repair_delivery_audit", "delivery_outcome"):
        assert _task(workflow, ref)["inputParameters"]["optionalPaths"] == "${workflow.input.acceptanceTests}"


def test_test_cycle_hands_the_declared_write_scope_to_discovery():
    """test_cycle received allowedWriteRoots but never passed it to test_discover, so a fork's repair
    agent was scoped to the discovered DIRECTORY (store/) and implemented sibling items' files to make
    the item's acceptance test pass; those commits then collided with the owners' forks (run a33d7061)."""
    workflow = _load("test_cycle")
    for ref in ("initial_discover", "discover"):
        assert _task(workflow, ref)["inputParameters"]["allowedWriteRoots"] == "${workflow.input.allowedWriteRoots}"
    assert _task(workflow, "repair")["inputParameters"]["allowedWriteRoots"] == "${initial_discover.output.repairScope}"
    from common import test_plan
    assert test_plan.repair_scope(["store/lists.go", "store/lists_test.go"], ["store"]) == \
        ["store/lists.go", "store/lists_test.go"]


def test_code_subtask_has_initial_pass_plus_three_data_driven_repairs():
    workflow = _load("code_subtask")
    initial = _task(workflow, "initial_delivery_audit")
    loop = _task(workflow, "delivery_repair_loop")
    repair = _task(workflow, "delivery_repair")
    context = _task(workflow, "delivery_repair_context")
    commit = _task(workflow, "cmt")
    outcome = _task(workflow, "delivery_outcome")

    assert initial["inputParameters"]["plannedPaths"] == \
        "${workflow.input.allowedWriteRoots}"
    assert "['iteration'] < 3" in loop["loopCondition"]
    assert "deliveryState !== 'passed'" in loop["loopCondition"]
    assert repair["optional"] is True
    assert repair["inputParameters"]["failSoft"] is True
    prompt = repair["inputParameters"]["prompt"]
    for field in ("missingPathsJson", "rejectedPathsJson", "commandFailuresJson",
                  "sandboxDenialsJson", "previousStructuredReportJson"):
        assert field in prompt
    # Serialization moved out of jq into the json_text worker: the task carried
    # a throwing-expression failure surface and three fixture cases for a row of
    # tojson calls and no logic.
    assert context["name"] == "json_text"
    assert context["type"] == "SIMPLE"
    assert set(context["inputParameters"]) == {
        "missingPaths", "rejectedPaths", "commandFailures", "sandboxDenials",
        "previousStructuredReport",
    }
    assert commit["optional"] is True
    assert outcome["inputParameters"]["commit"] == "${cmt.output}"
    assert outcome["name"] == "code_subtask_delivery_outcome"
    from common import code_subtask as code_subtask_logic
    result = code_subtask_logic.resolve_delivery_outcome(
        planned_paths=["a.py", "b.py"], commit={"stagedPaths": ["a.py"], "rejectedPaths": []},
        audit={}, agent={}, tested=None, test_state=None, tests_passed=None)
    assert result["missingPaths"] == ["b.py"]


def test_code_parallel_fail_soft_states_and_partial_merge_routing():
    from common import code_parallel

    workflow = _load("code_parallel")
    build = _task(workflow, "build_forks")
    # build_forks itself is optional:true on the dynamic tasks it emits, not on
    # the (now-worker) task that emits them.
    assert build["name"] == "code_parallel_build_forks"
    forks = code_parallel.build_forks(
        repo_path="/tmp", subtasks=[{"id": "a", "description": "d", "files": ["a.py"]}],
        change_dir="", code_model="", code_prompt_template="", code_prompt_template_source="",
        spec_context_path="", context_paths=[], max_turns=1, max_budget_usd=1,
        model_profile="", model_policy={}, model_policy_source="", model_policy_sha256="",
        models_config={}, model_overrides={})
    assert forks["dynamicTasks"][0]["optional"] is True
    assert _task(workflow, "fan_out")["optional"] is True
    assert _task(workflow, "fan_join")["optional"] is True
    assert _task(workflow, "merge")["optional"] is True

    for state, delivery_state, merge_state, plan_valid in (
        ("passed", "passed", "merged", True),
        ("incomplete_delivery", "incomplete_delivery", "merged", True),
        ("merge_blocked", "passed", "merge_blocked", True),
        ("audit_unavailable", "audit_unavailable", "merged", True),
        ("implementation_unavailable", "implementation_unavailable", "merged", True),
    ):
        outcome = code_parallel.resolve_verification_outcome(
            candidate_commit="c", delivery={"state": delivery_state}, issues=[],
            merge_state=merge_state, plan_valid=plan_valid, tested=None,
            test_state=None, tests_passed=None)
        assert outcome["state"] == state, state


def test_transient_publication_workers_use_three_exponential_retries():
    for name in ("issue_fetch", "git_clone", "workspace_prepare", "git_push", "pr_create"):
        definition = json.loads((WORKFLOWS / "taskdefs" / f"{name}.json").read_text())
        assert definition["retryCount"] == 3
        assert definition["retryLogic"] == "EXPONENTIAL_BACKOFF"


def test_delivery_audit_lists_each_file_of_a_new_directory(tmp_git_repo, fake_task_input):
    """The first files of a NEW directory showed up in git status as one collapsed entry ``resp/``, so
    the audit reported every planned file of the item missing and three no-op repair agents ran per
    such fork — resp-errors, resp-writer, store-core, server-dispatch (run ec0fb6dc)."""
    (tmp_git_repo / "resp").mkdir()
    (tmp_git_repo / "resp" / "errors.go").write_text("package resp\n")
    (tmp_git_repo / "resp" / "errors_test.go").write_text("package resp\n")
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["resp/errors.go", "resp/errors_test.go"],
        agentReport={"status": "success", "agentCompleted": True})).output_data
    assert out["state"] == "passed" and out["missingPaths"] == []
    assert out["actualPaths"] == ["resp/errors.go", "resp/errors_test.go"]


def test_delivery_audit_counts_a_planned_path_already_on_the_branch_as_delivered(tmp_git_repo, fake_task_input):
    """A review re-dispatch changes only what the reviewer asked for; the item's other files were
    delivered by its earlier attempt and are on the branch already. store-hashes changed only
    hashes_test.go and ran three no-op repair agents for the hashes.go it had delivered a wave earlier,
    then ended incomplete_delivery (run ec0fb6dc)."""
    from common import git
    (tmp_git_repo / "store").mkdir()
    (tmp_git_repo / "store" / "hashes.go").write_text("package store\n")
    git.git(str(tmp_git_repo), "add", "store/hashes.go")
    git.git(str(tmp_git_repo), "commit", "-q", "-m", "earlier attempt")
    (tmp_git_repo / "store" / "hashes_test.go").write_text("package store\n")
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["store/hashes.go", "store/hashes_test.go"],
        agentReport={"status": "success", "agentCompleted": True})).output_data
    assert out["state"] == "passed" and out["missingPaths"] == []
    assert out["presentPaths"] == ["store/hashes.go"] and out["actualPaths"] == ["store/hashes_test.go"]
    # A planned file that exists nowhere is still missing.
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["store/hashes.go", "store/hashes_test.go", "store/new.go"],
        agentReport={"status": "success", "agentCompleted": True})).output_data
    assert out["state"] == "incomplete_delivery" and out["missingPaths"] == ["store/new.go"]
    from common import code_subtask as code_subtask_logic
    result = code_subtask_logic.resolve_delivery_outcome(
        planned_paths=["store/hashes.go", "store/hashes_test.go"],
        commit={"stagedPaths": ["store/hashes_test.go"], "rejectedPaths": [], "commit": "abc", "noOp": False},
        audit={"presentPaths": ["store/hashes.go"]}, agent={"agentCompleted": True},
        tested=None, test_state=None, tests_passed=None)
    assert result["state"] == "passed" and result["missingPaths"] == []
    assert result["presentPaths"] == ["store/hashes.go"]


def test_delivery_audit_reports_a_pushback_and_the_workflow_runs_no_repair(tmp_git_repo, fake_task_input):
    """An implementer that pushed back on a directive (H16) made its case instead of the change; the Lead
    decides at integration. resp-reader's pushback was followed by a $0.57 repair agent that rewrote
    reader.go anyway, and the Lead then discarded that fork (run ec0fb6dc)."""
    report = {"status": "success", "agentCompleted": True,
              "structured": {"summary": "no change", "pushback": {"directive": "widen to docs", "reason": "out of scope"}}}
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["resp/reader.go"], agentReport=report)).output_data
    assert out["state"] == "pushback" and out["complete"] is False
    workflow = _load("code_subtask")
    gate = _task(workflow, "delivery_repair_gate")
    assert [t["type"] for t in gate["decisionCases"]["pushback"]] == ["SET_VARIABLE"]
    assert all(t["type"] != "DO_WHILE" for t in gate["decisionCases"]["pushback"])
    from common import code_subtask as code_subtask_logic
    result = code_subtask_logic.resolve_delivery_outcome(
        planned_paths=["resp/reader.go"], commit={"stagedPaths": [], "rejectedPaths": [], "noOp": True},
        audit={"state": "pushback", "presentPaths": ["resp/reader.go"]}, agent={"agentCompleted": True},
        tested=None, test_state=None, tests_passed=None)
    assert result["state"] == "pushback" and result["passed"] is False


def test_delivery_audit_treats_a_satisfied_claim_as_no_delivery_to_repair(tmp_git_repo, fake_task_input):
    """cmd-dispatch's implementer found its registry and dispatch already written by a sibling inside
    that sibling's file and changed nothing; the audit ran six repair agents that created nothing, then
    the item blocked as an empty delivery (run 90e5e3aa). A structured claim is no delivery to repair:
    the peer reviewer judges it at integration."""
    report = {"status": "success", "agentCompleted": True,
              "structured": {"summary": "no change",
                             "alreadySatisfied": {"evidence": "internal/command/handlers.go holds Registry and Dispatch"}}}
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["internal/command/command.go"], agentReport=report)).output_data
    assert out["state"] == "satisfied" and out["complete"] is False
    workflow = _load("code_subtask")
    gate = _task(workflow, "delivery_repair_gate")
    assert [t["type"] for t in gate["decisionCases"]["satisfied"]] == ["SET_VARIABLE"]
    from common import code_subtask as code_subtask_logic
    result = code_subtask_logic.resolve_delivery_outcome(
        planned_paths=["internal/command/command.go"], commit={"stagedPaths": [], "rejectedPaths": [], "noOp": True},
        audit={"state": "satisfied"}, agent={"agentCompleted": True}, tested=None, test_state=None, tests_passed=None)
    assert result["state"] == "satisfied" and result["passed"] is False
    # A claim made alongside actual changes is an ordinary delivery, audited on its files.
    (tmp_git_repo / "internal" / "command").mkdir(parents=True)
    (tmp_git_repo / "internal" / "command" / "command.go").write_text("package command\n")
    changed = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["internal/command/command.go"], agentReport=report)).output_data
    assert changed["state"] == "passed"


def test_delivery_audit_keeps_a_planned_dotfile(tmp_git_repo, fake_task_input):
    """`.gitignore` was normalised to `gitignore` (str.lstrip strips characters), so the audit never
    found it and three repair agents each created a bogus `gitignore` file (run f83293f5)."""
    from gitops.tasks import _repo_paths, delivery_audit
    assert _repo_paths([".gitignore", "./src/a.py", "././b"]) == [".gitignore", "b", "src/a.py"]
    with open(os.path.join(tmp_git_repo, ".gitignore"), "w") as fh:
        fh.write("node_modules\n")
    out = delivery_audit(fake_task_input(repoPath=tmp_git_repo, plannedPaths=[".gitignore"], agentReport={})).output_data
    assert out["state"] == "passed" and out["missingPaths"] == [] and out["actualPaths"] == [".gitignore"]


# --- run a87e9ddb: a truncated `git status` hid six written files from the commit and the audit ---------

def test_harness_parsed_output_is_never_cut(tmp_path):
    """The check runner keeps a 1 MB tail of a check's log. The harness's own commands are parsed, so
    they are kept whole: a 2.8 MB `git status` cut to its tail lost every backend path (run a87e9ddb)."""
    from common import exec as exec_mod
    from common.check_execution import execute, inherited_environment
    script = "import sys; sys.stdout.write('x' * 2_500_000)"
    assert len(exec_mod.run(["python3", "-c", script], cwd=str(tmp_path)).stdout) == 2_500_000
    logged = execute(["python3", "-c", script], cwd=str(tmp_path), env=inherited_environment())
    assert logged.output_truncated and len(logged.stdout) == 1_000_000


def test_generated_directories_stay_out_of_gits_view(tmp_git_repo):
    """An empty frontend/.gitignore and a team's `npm install` put 15,000 untracked node_modules files in
    a shared tree (run a87e9ddb). The harness excludes generated directories itself; a tracked file under
    such a directory stays tracked."""
    from common import git
    (tmp_git_repo / "build").mkdir()
    (tmp_git_repo / "build" / "kept.txt").write_text("v1\n")
    git.git(str(tmp_git_repo), "add", "-f", "build/kept.txt")
    git.git(str(tmp_git_repo), "commit", "-m", "tracked build file")
    (tmp_git_repo / "frontend" / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_git_repo / "frontend" / "node_modules" / "pkg" / "index.js").write_text("x\n")
    (tmp_git_repo / "backend").mkdir()
    (tmp_git_repo / "backend" / "pom.xml").write_text("<project/>\n")
    (tmp_git_repo / "build" / "kept.txt").write_text("v2\n")
    git.exclude_generated(str(tmp_git_repo))
    assert git.status_files(str(tmp_git_repo)) == {"backend/pom.xml", "build/kept.txt"}


def test_rejected_paths_travel_as_a_count_and_a_sample(tmp_git_repo):
    """15,000 rejected node_modules paths rode in every attempt's output, joins copied them, and the team
    workflow passed Conductor's 10 MB payload limit (run a87e9ddb)."""
    from common import git
    from common.results import FIELD_BYTES_LIMIT, oversized_fields
    nm = tmp_git_repo / "node_modules"
    nm.mkdir()
    for n in range(50):
        (nm / f"f{n}.js").write_text("x\n")
    out = git.commit(str(tmp_git_repo), "nothing real")
    assert out["noOp"] and out["rejectedCount"] == 50 and len(out["rejectedPaths"]) == git.PATH_SAMPLE
    assert oversized_fields({"small": [1, 2], "big": "y" * (FIELD_BYTES_LIMIT + 1)}) == [
        {"field": "big", "bytes": FIELD_BYTES_LIMIT + 3}]


def test_files_on_disk_git_does_not_report_are_a_staging_fault_not_a_missing_delivery(
        tmp_git_repo, fake_task_input, monkeypatch):
    """The backend scaffold wrote its six files; a truncated status hid them, the audit called them
    missing, and eight agents in turn were sent to write them (run a87e9ddb). Files on disk that git does
    not report are named as the harness's fault, and no repair runs."""
    from common import code_subtask, git
    (tmp_git_repo / "backend").mkdir()
    (tmp_git_repo / "backend" / "pom.xml").write_text("<project/>\n")
    monkeypatch.setattr(git, "vetted_changes", lambda repo: {"changed": [], "allowed": [], "rejected": []})
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["backend/pom.xml"],
        agentReport={"status": "success", "agentCompleted": True})).output_data
    assert out["state"] == "staging_fault" and out["stagingFault"] == ["backend/pom.xml"]
    outcome = code_subtask.resolve_delivery_outcome(
        planned_paths=["backend/pom.xml"], commit={"stagedPaths": []}, audit=out, agent={"agentCompleted": True},
        tested="", test_state="not_run", tests_passed=False)
    assert outcome["state"] == "staging_fault" and outcome["passed"] is False


def test_a_planned_file_the_repository_ignores_is_named_with_its_rule(tmp_git_repo, fake_task_input):
    """A planned file hidden by the repository's own ignore rules is fixable by the item: the audit says
    which rule, and the repair agent gets it as a failure to fix."""
    (tmp_git_repo / ".gitignore").write_text("*.properties\n")
    (tmp_git_repo / "app.properties").write_text("port=1\n")
    out = delivery_audit(fake_task_input(
        repoPath=str(tmp_git_repo), plannedPaths=["app.properties"],
        agentReport={"status": "success", "agentCompleted": True})).output_data
    assert out["state"] == "incomplete_delivery" and out["stagingFault"] == []
    assert out["ignoredPlanned"][0]["path"] == "app.properties" and "*.properties" in out["ignoredPlanned"][0]["rule"]
    assert any("ignored by" in f["stderr"] for f in out["commandFailures"])


def test_code_subtask_carries_the_audit_fields_the_outcome_reads():
    """The outcome reads presentPaths (files an earlier attempt committed) and stagingFault from the
    audit, so the workflow variable must carry both; presentPaths had been dropped, the same wiring
    defect as actualPaths in run 07808701."""
    wf = json.loads((Path(__file__).resolve().parents[1] / "workflows" / "code_subtask.json").read_text())
    text = json.dumps(wf)
    for prefix in ("initial_delivery_audit", "repair_delivery_audit"):
        for field in ("presentPaths", "stagingFault", "actualPaths"):
            assert f'"{field}": "${{{prefix}.output.{field}}}"' in text, (prefix, field)
    assert wf["outputParameters"]["stagingFault"] == "${delivery_outcome.output.stagingFault}"


def test_every_registered_worker_task_runs_the_function_its_name_says():
    """A helper inserted between `@worker_task(task_definition_name="delivery_audit")` and
    `def delivery_audit` became the registered worker: every audit of proof run e8293d55 failed with
    "unexpected keyword argument 'repo'", while tests that called delivery_audit directly passed. A
    worker's registered function must take the task, and an audit's name must be the function's."""
    import inspect
    import gitops.tasks  # noqa: F401  (registers its workers)
    from conductor.client.automator.task_handler import _decorated_functions
    for (name, _domain), record in _decorated_functions.items():
        fn = inspect.unwrap(record["func"])
        params = list(inspect.signature(fn).parameters)
        assert params[:1] == ["task"] or len(params) == 1, (name, fn.__name__, params)
    audit = next(r for (n, _d), r in _decorated_functions.items() if n == "delivery_audit")
    assert inspect.unwrap(audit["func"]).__name__ == "delivery_audit"
