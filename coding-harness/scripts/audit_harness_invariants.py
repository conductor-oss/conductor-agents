#!/usr/bin/env python3
"""Independent static and mutation audit of coding-harness workflow invariants.

This intentionally does not import the test suite or reuse test fixtures.  It
derives safety properties from workflow outcomes, inspects every nested control
path, then mutates valid definitions in memory to prove each important rule is
capable of detecting the corresponding regression.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / "workers" / "workflows"
TASKDEF_DIR = WORKFLOW_DIR / "taskdefs"

# Some invariants used to be checkable purely from workflow JSON because the
# logic lived in a JSON_JQ_TRANSFORM expression. Moving that logic into a
# worker (see the JQ Usage Policy in AGENTS.md) means the check must read the
# Python source instead. A mutation probe cannot edit definitions.py-imported
# source on disk, so it substitutes a mutated copy here and every read goes
# through this indirection.
_SOURCE_OVERRIDES: dict[str, str] = {}


def _module_source(relative_path: str) -> str:
    override = _SOURCE_OVERRIDES.get(relative_path)
    if override is not None:
        return override
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    """Return one top-level function's text, for a narrow substring check."""
    match = re.search(rf"^def {re.escape(name)}\(.*?(?=^def |\Z)", source, re.M | re.S)
    return match.group(0) if match else ""


@dataclass(frozen=True)
class Finding:
    code: str
    workflow: str
    location: str
    message: str


def nested_tasks(tasks: Iterable[dict], location: str = "tasks") -> Iterable[tuple[dict, str]]:
    for index, task in enumerate(tasks or []):
        if not isinstance(task, dict):
            continue
        here = f"{location}[{index}]"
        yield task, here
        for branch_index, branch in enumerate(task.get("forkTasks") or []):
            yield from nested_tasks(branch, f"{here}.forkTasks[{branch_index}]")
        for case, branch in (task.get("decisionCases") or {}).items():
            yield from nested_tasks(branch, f"{here}.decisionCases[{case!r}]")
        yield from nested_tasks(task.get("defaultCase") or [], f"{here}.defaultCase")
        yield from nested_tasks(task.get("loopOver") or [], f"{here}.loopOver")


def all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from all_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_strings(child)


def load_definitions() -> dict[str, dict]:
    definitions: dict[str, dict] = {}
    for path in sorted(WORKFLOW_DIR.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not value.get("name"):
            raise ValueError(f"{path}: expected one named workflow object")
        definitions[value["name"]] = value
    return definitions


def task_definition_names() -> set[str]:
    names: set[str] = set()
    for path in TASKDEF_DIR.glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        values = value if isinstance(value, list) else [value]
        names.update(str(item["name"]) for item in values if isinstance(item, dict) and item.get("name"))
    return names


REQUIRED_SCOPED_WRITERS = {
    ("campaign_subtask", "campaign_code"),
    ("code_subtask", "code"),
    ("code_parallel", "verify_fixup"),
    ("test_cycle", "repair"),
    ("design_docs", "design"),
    ("feature_campaign", "campaign_design"),
    ("feature_campaign", "adopt_wave_edits"),
    ("feature_campaign", "revise_wave_work"),
    ("feature_campaign", "final_verifier"),
    ("openspec_generate_artifact", "write"),
}


def audit_definitions(definitions: dict[str, dict]) -> list[Finding]:
    findings: list[Finding] = []
    taskdefs = task_definition_names()

    def report(code: str, workflow: str, location: str, message: str) -> None:
        findings.append(Finding(code, workflow, location, message))

    for name, definition in sorted(definitions.items()):
        tasks = list(nested_tasks(definition.get("tasks") or []))
        refs: dict[str, str] = {}
        has_push = False
        has_pr = False
        for task, location in tasks:
            reference = str(task.get("taskReferenceName") or "")
            if not reference:
                report("TASK_REF_MISSING", name, location, "taskReferenceName is required")
            elif reference in refs:
                report("TASK_REF_DUPLICATE", name, location,
                       f"{reference!r} duplicates {refs[reference]}")
            else:
                refs[reference] = location

            task_type = str(task.get("type") or "")
            task_name = str(task.get("name") or "")
            inputs = task.get("inputParameters") or {}
            if task_type == "SIMPLE" and task_name not in taskdefs:
                report("SIMPLE_TASKDEF_MISSING", name, location,
                       f"SIMPLE task {task_name!r} has no local task definition")
            if task_type == "DO_WHILE":
                condition = str(task.get("loopCondition") or "")
                if task.get("evaluatorType") != "graaljs":
                    report("LOOP_EVALUATOR", name, location, "DO_WHILE must explicitly use graaljs")
                if "iteration" not in condition or not re.search(r"iteration[^<]{0,40}(?:<|<=)", condition):
                    report("LOOP_BOUNDED", name, location, "DO_WHILE lacks an iteration upper bound")
                script_names = set(re.findall(r"\$\.([A-Za-z_][A-Za-z0-9_]*)", condition))
                missing = script_names - set(inputs)
                if missing:
                    report("SCRIPT_INPUT_SCOPE", name, location,
                           "DO_WHILE script variables are not declared inputs: " + ", ".join(sorted(missing)))
            if task_type == "SWITCH":
                evaluator = str(task.get("evaluatorType") or "value-param")
                expression = str(task.get("expression") or "")
                if evaluator in {"graaljs", "javascript"}:
                    script_names = set(re.findall(r"\$\.([A-Za-z_][A-Za-z0-9_]*)", expression))
                    missing = script_names - set(inputs)
                    if missing:
                        report("SCRIPT_INPUT_SCOPE", name, location,
                               "SWITCH script variables are not declared inputs: " + ", ".join(sorted(missing)))
                elif evaluator == "value-param" and expression not in inputs:
                    report("SWITCH_INPUT_SCOPE", name, location,
                           f"value-param SWITCH expression {expression!r} is not an input key")
            if task_type == "SUB_WORKFLOW":
                sub = task.get("subWorkflowParam") or {}
                child = sub.get("name")
                if child == "publish_verified_pr":
                    has_pr = True
                if child in definitions:
                    if not isinstance(sub.get("version"), int):
                        report("SUBWORKFLOW_VERSION", name, location,
                               f"local sub-workflow {child!r} is not version-pinned")
                    elif sub["version"] != definitions[child].get("version", 1):
                        report("SUBWORKFLOW_VERSION", name, location,
                               f"local sub-workflow {child!r} version does not match source")
            if task_name == "git_push":
                has_push = True
                if not inputs.get("expectedHead"):
                    report("PUSH_EXPECTED_HEAD", name, location,
                           "git_push must bind publication to the verified candidate SHA")
                if not inputs.get("branch"):
                    report("PUSH_BRANCH", name, location, "git_push must name the retained branch")
            if task_name == "pr_create":
                has_pr = True
                body = str(inputs.get("body") or "")
                if re.search(r"(?im)^#{1,6}\s*(?:cost|tokens?)\b", body):
                    report("PR_COST_SECTION", name, location,
                           "generated PR body contains a cost/token section")
            if (name, reference) in REQUIRED_SCOPED_WRITERS and not inputs.get("allowedWriteRoots"):
                report("WRITER_SCOPE", name, location,
                       "planned writer must receive explicit allowedWriteRoots")
            if task_name == "verification_run":
                for field in ("candidateCommit", "commands", "discoveryOutcome"):
                    if not inputs.get(field):
                        report("VERIFICATION_EVIDENCE", name, location,
                               f"verification_run is missing {field}")

        outputs = definition.get("outputParameters") or {}
        referenced: set[str] = set()
        input_references: set[str] = set()
        variable_references: set[str] = set()
        for value in all_strings(definition):
            referenced.update(re.findall(r"\$\{([A-Za-z0-9_-]+)\.output(?:[.}]|$)", value))
            input_references.update(re.findall(r"\$\{workflow\.input\.([A-Za-z0-9_-]+)", value))
            variable_references.update(re.findall(r"\$\{workflow\.variables\.([A-Za-z0-9_-]+)", value))
        for reference in sorted(referenced - set(refs)):
            report("TASK_OUTPUT_REFERENCE", name, "workflow",
                   f"expression references unknown task output {reference!r}")
        undeclared_inputs = input_references - set(definition.get("inputParameters") or [])
        for field in sorted(undeclared_inputs):
            report("WORKFLOW_INPUT_REFERENCE", name, "workflow",
                   f"expression references undeclared workflow input {field!r}")
        undeclared_variables = variable_references - set((definition.get("variables") or {}).keys())
        for field in sorted(undeclared_variables):
            report("WORKFLOW_VARIABLE_REFERENCE", name, "workflow",
                   f"expression references uninitialized workflow variable {field!r}")
        output_references: set[str] = set()
        for value in all_strings(outputs):
            output_references.update(re.findall(r"\$\{([A-Za-z0-9_-]+)\.output(?:[.}]|$)", value))
        conditional_refs = {
            str(task.get("taskReferenceName"))
            for task, location in tasks
            if ".decisionCases[" in location or ".defaultCase" in location
        }
        for reference in sorted(output_references & conditional_refs):
            report("CONDITIONAL_OUTPUT_REFERENCE", name, "outputParameters",
                   f"output reads conditional task {reference!r} directly; capture it in a workflow variable")
        if any(task.get("name") == "workspace_prepare" for task, _ in tasks):
            for key in ("branch", "sourceHandoff"):
                if key not in outputs:
                    report("LOCAL_HANDOFF_OUTPUT", name, "outputParameters",
                           f"local workspace workflow must expose {key}")
        if has_push or has_pr:
            for key in ("branch", "pushed", "publicationState"):
                if key not in outputs:
                    report("PUBLICATION_OUTPUT", name, "outputParameters",
                           f"publication workflow must expose {key}")

        # Every model-produced parallel plan is deterministically validated before
        # it can become dynamic Conductor tasks.
        top_refs = [task.get("taskReferenceName") for task in definition.get("tasks") or []]
        if name == "code_parallel":
            # plan_outcome was a JSON_JQ_TRANSFORM that folded planValid into a
            # {state} object for plan_gate to read; plan_gate now routes on the
            # workflow.variables.planValid boolean directly, so the checkpoint
            # ahead of the fan-out is plan_gate itself.
            required_order = ("plan_loop", "plan_gate", "build_forks", "fan_out")
            try:
                positions = [top_refs.index(ref) for ref in required_order]
            except ValueError:
                report("PLAN_GATE", name, "tasks", "parallel plan validation gate is missing")
            else:
                if positions != sorted(positions):
                    report("PLAN_GATE", name, "tasks",
                           "parallel plan reaches fan-out before deterministic validation")
            outcome = next((task for task, _ in tasks
                            if task.get("taskReferenceName") == "verification_outcome"), {})
            expression = str((outcome.get("inputParameters") or {}).get("queryExpression") or "")
            if expression:
                merge_fold_ok = "mergeState" in expression and "merged" in expression
            else:
                # Moved to common/code_parallel.py:resolve_verification_outcome.
                # "merged" and "merge_state" each recur elsewhere in the function
                # (the merge_blocked branch), so the check must be the specific
                # conjunction, not mere co-occurrence.
                body = _function_body(_module_source("workers/common/code_parallel.py"),
                                      "resolve_verification_outcome")
                merge_fold_ok = 'merge_state == "merged"' in body
            if not merge_fold_ok:
                report("VERIFY_MERGE_FOLD", name, "verification_outcome",
                       "final verification does not fold partial/conflicted merge state into failure")
            fork_builder = next((task for task, _ in tasks
                                 if task.get("taskReferenceName") == "build_forks"), {})
            query = str((fork_builder.get("inputParameters") or {}).get("queryExpression") or "")
            subtask = definitions.get("code_subtask") or {}
            subtask_text = "\n".join(all_strings(subtask))
            if query:
                scope_ok = "allowedWriteRoots" in query
            else:
                # Moved to common/code_parallel.py:build_forks. "allowedWriteRoots"
                # also names the function's own flattened return field, so the
                # check must be the specific per-subtask assignment.
                body = _function_body(_module_source("workers/common/code_parallel.py"),
                                      "build_forks")
                scope_ok = '"allowedWriteRoots": files' in body
            if not scope_ok or "stagedPaths" not in subtask_text:
                report("DELIVERY_FILE_GATE", name, "build_forks",
                       "planned exact files must reach the subtask and be checked against staged paths")

        if name == "test_cycle":
            text = "\n".join(all_strings(definition))
            required_signals = {
                "requiredVerificationCommands": "requiredVerificationCommands" in outputs,
                "canonical commands envelope": ".commands" in text
                and "verificationCommands" not in text,
                # The scope a repair may write to is now resolved by
                # test_discover and consumed as repairScope, rather than by a
                # separate jq task named repair_scope.
                "repair scope reaches the fix agent": "repairScope" in text,
            }
            for required, present in required_signals.items():
                if not present:
                    report("CUMULATIVE_VERIFICATION", name, "workflow",
                           f"remediation does not preserve {required}")

        # No planner may smuggle a shell pipeline into the verification contract.
        for value in all_strings(definition):
            if "testCmd" in value and re.search(r"testCmd[^\n]{0,100}\s&&\s", value):
                report("SHELL_COMPOSED_CHECK", name, "workflow",
                       "planner checks are recombined with shell &&")

    return findings


def mutation_probe(definitions: dict[str, dict]) -> list[str]:
    """Prove the auditor fails when each central safety control is removed."""
    probes: list[tuple[str, str, Any]] = []

    def remove_push_head(values: dict[str, dict]) -> None:
        for task, _ in nested_tasks(values["publish_verified_pr"]["tasks"]):
            if task.get("name") == "git_push":
                task["inputParameters"].pop("expectedHead", None)
                return

    def unbound_loop(values: dict[str, dict]) -> None:
        for task, _ in nested_tasks(values["test_cycle"]["tasks"]):
            if task.get("type") == "DO_WHILE":
                task["loopCondition"] = "(function(){ return true; })();"
                return

    def remove_scope(values: dict[str, dict]) -> None:
        for task, _ in nested_tasks(values["code_subtask"]["tasks"]):
            if task.get("taskReferenceName") == "code":
                task["inputParameters"].pop("allowedWriteRoots", None)
                return

    def remove_publication_output(values: dict[str, dict]) -> None:
        values["github_demo"]["outputParameters"].pop("branch", None)

    def remove_merge_fold(values: dict[str, dict]) -> None:
        for task, _ in nested_tasks(values["code_parallel"]["tasks"]):
            if task.get("taskReferenceName") == "verification_outcome" and \
                    "queryExpression" in (task.get("inputParameters") or {}):
                task["inputParameters"]["queryExpression"] = "{passed:.verifyPassed,state:.verifyState}"
                return
        # verification_outcome is a worker now: mutate the source the check
        # reads instead, so this probe still proves VERIFY_MERGE_FOLD fires.
        source = _module_source("workers/common/code_parallel.py")
        marker = 'merge_state == "merged"'
        assert marker in source, "mutation probe target moved; update remove_merge_fold"
        _SOURCE_OVERRIDES["workers/common/code_parallel.py"] = source.replace(marker, "True")

    def remove_delivery_file_gate(values: dict[str, dict]) -> None:
        for task, _ in nested_tasks(values["code_parallel"]["tasks"]):
            if task.get("taskReferenceName") == "build_forks" and \
                    "queryExpression" in (task.get("inputParameters") or {}):
                query = task["inputParameters"]["queryExpression"]
                task["inputParameters"]["queryExpression"] = query.replace(
                    "allowedWriteRoots", "unscopedFiles")
                return
        source = _module_source("workers/common/code_parallel.py")
        marker = '"allowedWriteRoots": files,'
        assert marker in source, "mutation probe target moved; update remove_delivery_file_gate"
        _SOURCE_OVERRIDES["workers/common/code_parallel.py"] = source.replace(
            marker, '"unscopedFiles": files,')

    def expose_conditional_output(values: dict[str, dict]) -> None:
        values["automation_dispatch"]["outputParameters"]["childWorkflowId"] = (
            "${start_child.output.workflowId}")

    def remove_local_branch_output(values: dict[str, dict]) -> None:
        values["code_parallel"]["outputParameters"].pop("branch", None)

    def break_script_scope(values: dict[str, dict]) -> None:
        # Must name an input the loop condition actually reads. A stale name
        # here mutates nothing, the auditor correctly reports no violation, and
        # the probe silently stops proving the check works.
        for task, _ in nested_tasks(values["test_cycle"]["tasks"]):
            if task.get("type") == "DO_WHILE":
                removed = task["inputParameters"].pop("maxFixAttempts", None)
                assert removed is not None, "SCRIPT_INPUT_SCOPE probe lost its target input"
                return

    probes.extend([
        ("PUSH_EXPECTED_HEAD", "remove verified push SHA", remove_push_head),
        ("LOOP_BOUNDED", "remove loop bound", unbound_loop),
        ("WRITER_SCOPE", "remove planned write scope", remove_scope),
        ("PUBLICATION_OUTPUT", "remove retained branch output", remove_publication_output),
        ("VERIFY_MERGE_FOLD", "ignore partial merge state", remove_merge_fold),
        ("DELIVERY_FILE_GATE", "drop exact delivery file gate", remove_delivery_file_gate),
        ("CONDITIONAL_OUTPUT_REFERENCE", "read an unexecuted branch output", expose_conditional_output),
        ("LOCAL_HANDOFF_OUTPUT", "remove local outcome branch", remove_local_branch_output),
        ("SCRIPT_INPUT_SCOPE", "remove a loop script input", break_script_scope),
    ])

    failures: list[str] = []
    for expected, label, mutate in probes:
        changed = copy.deepcopy(definitions)
        _SOURCE_OVERRIDES.clear()
        mutate(changed)
        observed = {finding.code for finding in audit_definitions(changed)}
        _SOURCE_OVERRIDES.clear()
        if expected not in observed:
            failures.append(f"mutation probe failed: {label} did not trigger {expected}")
    return failures


def main() -> int:
    definitions = load_definitions()
    findings = audit_definitions(definitions)
    mutation_failures = mutation_probe(definitions)
    task_count = sum(1 for definition in definitions.values()
                     for _ in nested_tasks(definition.get("tasks") or []))
    loop_count = sum(1 for definition in definitions.values()
                     for task, _ in nested_tasks(definition.get("tasks") or [])
                     if task.get("type") == "DO_WHILE")
    print(f"audited {len(definitions)} workflows, {task_count} nested tasks, {loop_count} loops")
    print("mutation probes: " + ("passed" if not mutation_failures else "FAILED"))
    for finding in findings:
        print(f"ERROR {finding.code} {finding.workflow}:{finding.location}: {finding.message}")
    for failure in mutation_failures:
        print(f"ERROR MUTATION {failure}")
    if findings or mutation_failures:
        return 1
    print("all independently derived static invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
