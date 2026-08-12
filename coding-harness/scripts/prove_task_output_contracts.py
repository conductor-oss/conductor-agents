#!/usr/bin/env python3
"""Audit every task-output-to-task-input contract against live Conductor history.

This is an operational evidence collector, not a unit test and not a mock-based
workflow run.  It inventories every ``${taskRef.output...}`` expression under a
task's ``inputParameters`` and proves the wiring with producer output and
resolved consumer input captured in the same real workflow execution.

Evidence is intentionally strict:

* local workflow version 1 must exactly match the registered contract catalog;
* the consumer task's recorded ``workflowTask`` must contain the current input
  expression, so executions created from stale definitions are ignored;
* the producer must have completed and the referenced output path must exist;
* an exact expression must equal the resolved consumer input; and
* each observed producer shape needs a completed consumer observation.

The generated report calls never-executed paths unproven.  It never substitutes
fixtures, mock task output, or prose assertions for runtime evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / "workers" / "workflows"
REPORT_DIR = ROOT / "reports"
BASE = os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080/api").rstrip("/")
TOKEN = os.environ.get("CONDUCTOR_AUTH_TOKEN", "")
EXPRESSION = re.compile(
    r"\$\{(?P<reference>[A-Za-z0-9_-]+)\.output(?:\.(?P<path>[^}]+))?\}"
)
TERMINAL_CONSUMER = {"COMPLETED"}
ACCEPTED_PRODUCER = {"COMPLETED", "COMPLETED_WITH_ERRORS"}
PROOF_EDGE_ALIASES = {
    "coding_harness_address_repair_contract_proof": [
        (
            "address_pr", "verify", "<root>", "repair",
            "inputParameters.verification",
        ),
    ],
}


@dataclass(frozen=True)
class Contract:
    workflow: str
    workflow_file: str
    producer_ref: str
    producer_name: str
    producer_type: str
    output_path: str
    consumer_ref: str
    consumer_name: str
    consumer_type: str
    input_path: str
    expression: str
    mode: str
    task_location: str
    consumer_input_sha256: str

    @property
    def key(self) -> tuple[str, ...]:
        return (
            self.workflow,
            self.producer_ref,
            self.output_path,
            self.consumer_ref,
            self.input_path,
            self.expression,
            self.consumer_input_sha256,
        )


@dataclass
class Observation:
    workflow_id: str
    workflow_status: str
    producer_runtime_ref: str
    producer_status: str
    producer_shape: str
    consumer_runtime_ref: str
    consumer_status: str
    consumer_shape: str
    verdict: str
    detail: str
    evidence_kind: str = "production"


def request(method: str, path: str, body: Any = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["X-Authorization"] = TOKEN
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:  # noqa: S310
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


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


def string_leaves(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from string_leaves(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}.{index}" if path else str(index)
            yield from string_leaves(child, child_path)


def load_workflows() -> dict[str, tuple[Path, dict]]:
    workflows: dict[str, tuple[Path, dict]] = {}
    for path in sorted(WORKFLOW_DIR.glob("*.json")):
        definition = json.loads(path.read_text(encoding="utf-8"))
        name = str(definition.get("name") or "")
        if not name:
            raise RuntimeError(f"{path}: workflow name is missing")
        if definition.get("version") != 1:
            raise RuntimeError(f"{path}: only workflow version 1 is allowed")
        if name in workflows:
            raise RuntimeError(f"duplicate workflow name {name!r}")
        workflows[name] = (path, definition)
    return workflows


def input_contract_sha256(inputs: Any) -> str:
    canonical = json.dumps(inputs or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def inventory(workflows: dict[str, tuple[Path, dict]]) -> tuple[list[Contract], list[dict]]:
    contracts: list[Contract] = []
    workflow_outputs: list[dict] = []
    for workflow, (path, definition) in sorted(workflows.items()):
        task_entries = list(nested_tasks(definition.get("tasks") or []))
        tasks_by_ref = {
            str(task.get("taskReferenceName") or ""): task
            for task, _location in task_entries
        }
        if "" in tasks_by_ref:
            raise RuntimeError(f"{path}: a task has no taskReferenceName")
        for consumer, location in task_entries:
            inputs = consumer.get("inputParameters") or {}
            for input_path, template in string_leaves(inputs, "inputParameters"):
                for match in EXPRESSION.finditer(template):
                    producer_ref = match.group("reference")
                    producer = tasks_by_ref.get(producer_ref)
                    if producer is None:
                        raise RuntimeError(
                            f"{path}:{location}.{input_path}: unknown producer {producer_ref!r}"
                        )
                    expression = match.group(0)
                    contracts.append(Contract(
                        workflow=workflow,
                        workflow_file=str(path.relative_to(ROOT)),
                        producer_ref=producer_ref,
                        producer_name=str(producer.get("name") or ""),
                        producer_type=str(producer.get("type") or ""),
                        output_path=match.group("path") or "<root>",
                        consumer_ref=str(consumer.get("taskReferenceName") or ""),
                        consumer_name=str(consumer.get("name") or ""),
                        consumer_type=str(consumer.get("type") or ""),
                        input_path=input_path,
                        expression=expression,
                        mode="exact" if template == expression else "template",
                        task_location=location,
                        consumer_input_sha256=input_contract_sha256(inputs),
                    ))
        for output_path, template in string_leaves(
            definition.get("outputParameters") or {}, "outputParameters"
        ):
            for match in EXPRESSION.finditer(template):
                workflow_outputs.append({
                    "workflow": workflow,
                    "producer": match.group("reference"),
                    "outputPath": match.group("path") or "<root>",
                    "workflowOutputPath": output_path,
                    "expression": match.group(0),
                })
    contracts.sort(key=lambda item: item.key)
    return contracts, workflow_outputs


def contract_signature(contracts: Iterable[Contract]) -> set[tuple[str, ...]]:
    return {contract.key for contract in contracts}


def workflow_output_signature(outputs: Iterable[dict]) -> set[tuple[str, str, str, str, str]]:
    return {
        (
            str(item["workflow"]),
            str(item["producer"]),
            str(item["outputPath"]),
            str(item["workflowOutputPath"]),
            str(item["expression"]),
        )
        for item in outputs
    }


def registered_inventory(
    local: dict[str, tuple[Path, dict]],
) -> tuple[list[Contract], list[dict]]:
    registered: dict[str, tuple[Path, dict]] = {}
    for name, (path, _definition) in sorted(local.items()):
        definition = request("GET", f"/metadata/workflow/{name}?version=1")
        if definition.get("version") != 1:
            raise RuntimeError(f"registered {name!r} is not version 1")
        registered[name] = (path, definition)
    return inventory(registered)


def path_parts(path: str) -> list[str]:
    return [] if path in {"", "<root>"} else path.split(".")


def lookup(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for part in path_parts(path):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def shape(value: Any, depth: int = 0) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    if isinstance(value, list):
        if not value:
            return "array[0]"
        child_shapes = sorted({shape(child, depth + 1) for child in value[:25]})
        children = "|".join(child_shapes[:4])
        return f"array[{len(value)}]<{children}>"
    if isinstance(value, dict):
        keys = sorted(str(key) for key in value)
        if depth >= 1:
            preview = ",".join(keys[:8])
            suffix = ",…" if len(keys) > 8 else ""
            return f"object{{{preview}{suffix}}}"
        fields = []
        for key in keys[:8]:
            fields.append(f"{key}:{shape(value[key], depth + 1)}")
        suffix = ",…" if len(keys) > 8 else ""
        return "object{" + ",".join(fields) + suffix + "}"
    return type(value).__name__


def execution_search(workflow_names: set[str]) -> list[dict]:
    query = urllib.parse.urlencode({
        "start": 0,
        "size": 5000,
        "sort": "startTime:DESC",
    })
    result = request("GET", f"/workflow/search?{query}")
    return [
        hit for hit in (result.get("results") or [])
        if str(hit.get("workflowType") or "") in workflow_names
    ]


def task_base_ref(task: dict) -> str:
    workflow_task = task.get("workflowTask") or {}
    return str(workflow_task.get("taskReferenceName") or task.get("referenceTaskName") or "")


def task_sequence(task: dict) -> int:
    value = task.get("seq")
    return int(value) if isinstance(value, (int, float)) else -1


def same_iteration(left: dict, right: dict) -> bool:
    left_iteration = left.get("iteration")
    right_iteration = right.get("iteration")
    if left_iteration is None or right_iteration is None:
        return True
    return left_iteration == right_iteration


def select_producer(tasks: list[dict], consumer: dict, producer_ref: str) -> dict | None:
    candidates = [task for task in tasks if task_base_ref(task) == producer_ref]
    if not candidates:
        return None
    if producer_ref == task_base_ref(consumer):
        return consumer
    before = [
        task for task in candidates
        if same_iteration(task, consumer) and task_sequence(task) <= task_sequence(consumer)
    ]
    if before:
        return max(before, key=task_sequence)
    earlier = [task for task in candidates if task_sequence(task) <= task_sequence(consumer)]
    if earlier:
        return max(earlier, key=task_sequence)
    return max(candidates, key=task_sequence)


def current_template(task: dict, contract: Contract, require_input_contract: bool = True) -> bool:
    workflow_task = task.get("workflowTask") or {}
    if str(workflow_task.get("type") or "") != contract.consumer_type:
        return False
    if str(workflow_task.get("name") or "") != contract.consumer_name:
        return False
    if (
        require_input_contract
        and input_contract_sha256(workflow_task.get("inputParameters") or {})
        != contract.consumer_input_sha256
    ):
        return False
    found, recorded = lookup(workflow_task, contract.input_path)
    if not found or not isinstance(recorded, str):
        return False
    return contract.expression in recorded


def resolved_consumer_input(task: dict, contract: Contract) -> tuple[bool, Any]:
    relative = contract.input_path.removeprefix("inputParameters")
    relative = relative.removeprefix(".")
    inputs = task.get("inputData") or {}
    if contract.consumer_type == "SUB_WORKFLOW":
        inputs = inputs.get("workflowInput") or {}
    return lookup(inputs, relative)


def observe(
    contract: Contract,
    execution: dict,
    require_input_contract: bool = True,
) -> list[Observation]:
    tasks = execution.get("tasks") or []
    consumers = [
        task for task in tasks
        if task_base_ref(task) == contract.consumer_ref
        and current_template(task, contract, require_input_contract=require_input_contract)
    ]
    observations: list[Observation] = []
    for consumer in consumers:
        consumer_status = str(consumer.get("status") or "")
        if consumer_status not in {"COMPLETED", "FAILED", "TIMED_OUT", "COMPLETED_WITH_ERRORS"}:
            continue
        producer = select_producer(tasks, consumer, contract.producer_ref)
        if producer is None:
            observations.append(Observation(
                workflow_id=str(execution.get("workflowId") or ""),
                workflow_status=str(execution.get("status") or ""),
                producer_runtime_ref="<missing>",
                producer_status="MISSING",
                producer_shape="missing",
                consumer_runtime_ref=str(consumer.get("referenceTaskName") or ""),
                consumer_status=str(consumer.get("status") or ""),
                consumer_shape="unknown",
                verdict="MISSING_PRODUCER",
                detail="consumer ran but no producer task instance was recorded",
            ))
            continue
        output_exists, output_value = lookup(
            producer.get("outputData"), contract.output_path
        )
        input_exists, input_value = resolved_consumer_input(consumer, contract)
        producer_status = str(producer.get("status") or "")
        if (
            contract.producer_ref == contract.consumer_ref
            and contract.consumer_type == "DO_WHILE"
            and consumer_status == "COMPLETED"
            and producer_status == "COMPLETED"
        ):
            verdict = "PROVEN"
            detail = "completed canonical DO_WHILE lazy self-reference"
            input_exists = True
            input_value = "<lazy-self-reference>"
        elif producer_status not in ACCEPTED_PRODUCER:
            verdict = "PRODUCER_NOT_COMPLETED"
            detail = f"producer status is {producer_status or 'unknown'}"
        elif not output_exists and input_exists and input_value is None and consumer_status == "COMPLETED":
            verdict = "PROVEN"
            detail = "absent optional producer field resolved to null and consumer completed"
        elif not output_exists:
            verdict = "MISSING_OUTPUT_PATH"
            detail = "the referenced output path is absent"
        elif not input_exists:
            verdict = "MISSING_CONSUMER_INPUT"
            detail = "the resolved consumer input path is absent"
        elif contract.mode == "exact" and output_value != input_value:
            verdict = "VALUE_MISMATCH"
            detail = "producer value does not equal resolved consumer input"
        elif consumer_status in TERMINAL_CONSUMER:
            verdict = "PROVEN"
            detail = (
                "exact value equality and completed consumer"
                if contract.mode == "exact"
                else "template resolved and consumer completed"
            )
        else:
            verdict = "RESOLVED_NOT_ACCEPTED"
            detail = f"wiring resolved but consumer status is {consumer_status or 'unknown'}"
        observations.append(Observation(
            workflow_id=str(execution.get("workflowId") or ""),
            workflow_status=str(execution.get("status") or ""),
            producer_runtime_ref=str(producer.get("referenceTaskName") or ""),
            producer_status=producer_status,
            producer_shape=shape(output_value) if output_exists else "missing",
            consumer_runtime_ref=str(consumer.get("referenceTaskName") or ""),
            consumer_status=consumer_status,
            consumer_shape=shape(input_value) if input_exists else "missing",
            verdict=verdict,
            detail=detail,
        ))
    return observations


def contract_verdict(observations: list[Observation]) -> tuple[str, str, Observation | None]:
    if not observations:
        return "UNEXERCISED", "no current-definition consumer task was recorded", None
    hard_failures = [
        observation for observation in observations
        if observation.verdict in {
            "MISSING_PRODUCER", "PRODUCER_NOT_COMPLETED", "MISSING_OUTPUT_PATH",
            "MISSING_CONSUMER_INPUT", "VALUE_MISMATCH",
        }
    ]
    if hard_failures:
        evidence = hard_failures[0]
        return "FAILED", evidence.detail, evidence
    by_shape: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        by_shape[observation.producer_shape].append(observation)
    unaccepted_shapes = [
        producer_shape for producer_shape, values in by_shape.items()
        if not any(value.verdict == "PROVEN" for value in values)
    ]
    if unaccepted_shapes:
        evidence = next(
            value for value in observations
            if value.producer_shape == unaccepted_shapes[0]
        )
        return (
            "RESOLVED_NOT_PROVEN",
            "no completed consumer for observed shape(s): " + ", ".join(unaccepted_shapes),
            evidence,
        )
    evidence = next(value for value in observations if value.verdict == "PROVEN")
    return "PROVEN", f"{len(observations)} same-execution observation(s)", evidence


def markdown_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_reports(
    contracts: list[Contract],
    workflow_outputs: list[dict],
    observations: dict[tuple[str, ...], list[Observation]],
    execution_count: int,
    markdown_path: Path,
    json_path: Path,
) -> dict:
    rows = []
    for contract in contracts:
        values = observations.get(contract.key, [])
        verdict, detail, evidence = contract_verdict(values)
        observed_producer_shapes = sorted({value.producer_shape for value in values})
        observed_consumer_shapes = sorted({value.consumer_shape for value in values})
        rows.append({
            **asdict(contract),
            "verdict": verdict,
            "detail": detail,
            "producerShapes": observed_producer_shapes,
            "consumerShapes": observed_consumer_shapes,
            "observationCount": len(values),
            "evidence": asdict(evidence) if evidence else None,
            "allObservations": [asdict(value) for value in values],
        })
    counts = Counter(row["verdict"] for row in rows)
    workflow_counts: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        workflow_counts[row["workflow"]][row["verdict"]] += 1
    summary = {
        "server": BASE,
        "workflowDefinitions": len({contract.workflow for contract in contracts}),
        "runtimeExecutionsInspected": execution_count,
        "taskInputContracts": len(contracts),
        "workflowOutputReferences": len(workflow_outputs),
        "verdicts": dict(sorted(counts.items())),
    }
    payload = {"summary": summary, "contracts": rows, "workflowOutputs": workflow_outputs}
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# Task Output Contract Evidence",
        "",
        "This report inventories every `${taskRef.output…}` expression wired into a task input. ",
        "`PROVEN` means the current registered expression was observed in a real execution, ",
        "the producer path existed, exact expressions matched the resolved consumer input, ",
        "and a consumer completed for every observed producer shape. Mocks and `/workflow/test` ",
        "executions are not evidence. `UNEXERCISED` is deliberately not treated as proof.",
        "",
        "## Summary",
        "",
        "| Measure | Count |",
        "|---|---:|",
        f"| Workflow definitions | {summary['workflowDefinitions']} |",
        f"| Real executions inspected | {execution_count} |",
        f"| Task-input contracts | {len(contracts)} |",
        f"| Workflow-output-only references | {len(workflow_outputs)} |",
    ]
    for verdict, count in sorted(counts.items()):
        lines.append(f"| {verdict} | {count} |")
    lines.extend([
        "",
        "## Coverage by workflow",
        "",
        "| Workflow | Contracts | Proven | Failed | Resolved, not proven | Unexercised |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for workflow, values in sorted(workflow_counts.items()):
        lines.append(
            f"| {markdown_escape(workflow)} | {sum(values.values())} | "
            f"{values['PROVEN']} | {values['FAILED']} | {values['RESOLVED_NOT_PROVEN']} | "
            f"{values['UNEXERCISED']} |"
        )
    lines.extend([
        "",
        "## Complete producer → consumer table",
        "",
        "| Workflow | Producer task and output | Producer shape(s) | Consumer task | Consumer input | Resolved input shape(s) | Same-execution evidence | Result |",
        "|---|---|---|---|---|---|---|---|",
    ])
    for row in rows:
        evidence = row["evidence"]
        if evidence:
            evidence_text = (
                f"{evidence['evidence_kind']} `{evidence['workflow_id']}`; "
                f"`{evidence['producer_runtime_ref']}` {evidence['producer_status']} → "
                f"`{evidence['consumer_runtime_ref']}` {evidence['consumer_status']}"
            )
        else:
            evidence_text = "none"
        producer = f"`{row['producer_ref']}.output"
        if row["output_path"] != "<root>":
            producer += f".{row['output_path']}"
        producer += "`"
        consumer_input = f"`{row['input_path']}` ({row['mode']})"
        lines.append(
            "| " + " | ".join([
                markdown_escape(row["workflow"]),
                markdown_escape(producer),
                markdown_escape(", ".join(row["producerShapes"]) or "not observed"),
                markdown_escape(f"`{row['consumer_ref']}` ({row['consumer_type']})"),
                markdown_escape(consumer_input),
                markdown_escape(", ".join(row["consumerShapes"]) or "not observed"),
                markdown_escape(evidence_text),
                markdown_escape(f"{row['verdict']}: {row['detail']}"),
            ]) + " |"
        )
    lines.extend([
        "",
        "## Workflow-output-only references",
        "",
        "These references expose task output as workflow output; they are not task-input contracts.",
        "",
        "| Workflow | Producer output | Workflow output field |",
        "|---|---|---|",
    ])
    for row in workflow_outputs:
        producer = f"{row['producer']}.output"
        if row["outputPath"] != "<root>":
            producer += f".{row['outputPath']}"
        lines.append(
            f"| {markdown_escape(row['workflow'])} | `{markdown_escape(producer)}` | "
            f"`{markdown_escape(row['workflowOutputPath'])}` |"
        )
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--markdown",
        type=Path,
        default=REPORT_DIR / "task-output-contracts.md",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=REPORT_DIR / "task-output-contracts.json",
    )
    args = parser.parse_args()

    workflows = load_workflows()
    contracts, workflow_outputs = inventory(workflows)
    registered_contracts, registered_outputs = registered_inventory(workflows)
    local_signature = contract_signature(contracts)
    registered_signature = contract_signature(registered_contracts)
    if local_signature != registered_signature:
        missing = sorted(local_signature - registered_signature)
        extra = sorted(registered_signature - local_signature)
        raise RuntimeError(
            "registered version-1 task-output catalog is stale: "
            f"missing={missing[:10]!r} extra={extra[:10]!r}"
        )
    local_output_signature = workflow_output_signature(workflow_outputs)
    registered_output_signature = workflow_output_signature(registered_outputs)
    if local_output_signature != registered_output_signature:
        missing = sorted(local_output_signature - registered_output_signature)
        extra = sorted(registered_output_signature - local_output_signature)
        raise RuntimeError(
            "registered version-1 workflow-output catalog is stale: "
            f"missing={missing[:10]!r} extra={extra[:10]!r}"
        )

    hits = execution_search(set(workflows) | set(PROOF_EDGE_ALIASES))
    executions = []
    excluded_mock_executions = 0
    for hit in hits:
        workflow_id = str(hit.get("workflowId") or "")
        if not workflow_id:
            continue
        execution = request("GET", f"/workflow/{workflow_id}?includeTasks=true")
        # Conductor's /workflow/test endpoint records a wildcard task domain and
        # may flatten SUB_WORKFLOW tasks into mocked SIMPLE tasks.  Those are
        # useful decision-engine checks, but the user explicitly requires real
        # producer/consumer evidence, so they cannot count here.
        task_to_domain = execution.get("taskToDomain") or {}
        if isinstance(task_to_domain, dict) and task_to_domain.get("*"):
            excluded_mock_executions += 1
            continue
        executions.append(execution)
    contracts_by_workflow: dict[str, list[Contract]] = defaultdict(list)
    for contract in contracts:
        contracts_by_workflow[contract.workflow].append(contract)
    observations: dict[tuple[str, ...], list[Observation]] = defaultdict(list)
    for execution in executions:
        workflow = str(execution.get("workflowName") or execution.get("workflowType") or "")
        if workflow in PROOF_EDGE_ALIASES:
            for source_workflow, producer_ref, output_path, consumer_ref, input_path in (
                PROOF_EDGE_ALIASES[workflow]
            ):
                matches = [
                    contract for contract in contracts_by_workflow[source_workflow]
                    if contract.producer_ref == producer_ref
                    and contract.output_path == output_path
                    and contract.consumer_ref == consumer_ref
                    and contract.input_path == input_path
                ]
                if len(matches) != 1:
                    raise RuntimeError(
                        f"operational proof alias {workflow!r} resolved to {len(matches)} contracts"
                    )
                proof_observations = observe(
                    matches[0], execution, require_input_contract=False
                )
                for proof_observation in proof_observations:
                    proof_observation.evidence_kind = "operational-proof"
                observations[matches[0].key].extend(proof_observations)
            continue
        for contract in contracts_by_workflow.get(workflow, []):
            observations[contract.key].extend(observe(contract, execution))

    summary = write_reports(
        contracts,
        workflow_outputs,
        observations,
        len(executions),
        args.markdown,
        args.json,
    )
    print(json.dumps({
        **summary,
        "mockExecutionsExcluded": excluded_mock_executions,
        "markdown": str(args.markdown),
        "json": str(args.json),
    }, indent=2, sort_keys=True))
    return 1 if summary["verdicts"].get("FAILED", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
