#!/usr/bin/env python3
"""Replay registered JQ transforms in Conductor with recorded runtime inputs.

This is an operational evidence command, not an in-process JQ evaluator.  It:

* requires every local expression to exactly match registered workflow v1;
* prefers the newest input recorded for that task in a real workflow run;
* explicitly labels catalog examples when a task has never run in production;
* executes every expression as a real JSON_JQ_TRANSFORM task in Conductor; and
* leaves the proof execution available for inspection.

No task with catalog-only evidence is represented as production-covered.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "workers" / "workflows"
FIXTURES = (
    ROOT / "workers" / "tests" / "fixtures" / "jq_conductor_cases.json",
    ROOT / "workers" / "tests" / "fixtures" / "jq_conductor_adversarial_cases.json",
    ROOT / "workers" / "tests" / "fixtures" / "jq_conductor_third_pass_cases.json",
)
BASE = os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080/api").rstrip("/")
TOKEN = os.environ.get("CONDUCTOR_AUTH_TOKEN", "")
PROOF_NAME = "coding_harness_jq_recorded_contract_proof"
TERMINAL = {"COMPLETED", "FAILED", "TIMED_OUT", "TERMINATED"}


def request(method: str, path: str, body=None):
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


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def catalog() -> dict[tuple[str, str], dict]:
    found = {}
    for path in sorted(WORKFLOWS.glob("*.json")):
        workflow = json.loads(path.read_text(encoding="utf-8"))
        for task in walk(workflow):
            if task.get("type") != "JSON_JQ_TRANSFORM":
                continue
            key = (workflow["name"], task["taskReferenceName"])
            if key in found:
                raise RuntimeError(f"duplicate JQ task identity: {key}")
            found[key] = task
    return found


def catalog_inputs() -> dict[tuple[str, str], dict]:
    inputs = {}
    for path in FIXTURES:
        for case in json.loads(path.read_text(encoding="utf-8")):
            key = (case["workflow"], case["task"])
            inputs.setdefault(key, case["input"])
    return inputs


def compact(value):
    """Bound evidence size without changing its JSON type structure."""
    if isinstance(value, dict):
        return {str(key): compact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [compact(item) for item in value[:50]]
    if isinstance(value, str):
        if "${" in value:
            return "recorded-template-value"
        return value[:2048]
    return value


def registered_expressions(local: dict[tuple[str, str], dict]) -> None:
    by_workflow: dict[str, dict[tuple[str, str], str]] = {}
    for workflow, _name in local:
        if workflow in by_workflow:
            continue
        definition = request("GET", f"/metadata/workflow/{workflow}?version=1")
        if definition.get("version") != 1:
            raise RuntimeError(f"{workflow} is not registered at version 1")
        expressions = {}
        for task in walk(definition):
            if task.get("type") == "JSON_JQ_TRANSFORM":
                expressions[(workflow, task["taskReferenceName"])] = \
                    task["inputParameters"]["queryExpression"]
        by_workflow[workflow] = expressions
    registered = {key: expression for expressions in by_workflow.values()
                  for key, expression in expressions.items()}
    if set(registered) != set(local):
        raise RuntimeError(
            f"registered JQ catalog differs: missing={sorted(set(local) - set(registered))} "
            f"extra={sorted(set(registered) - set(local))}"
        )
    for key, task in local.items():
        if registered[key] != task["inputParameters"]["queryExpression"]:
            raise RuntimeError(f"registered expression is stale: {key[0]}/{key[1]}")


def recorded_inputs(local: dict[tuple[str, str], dict]) -> dict[tuple[str, str], dict]:
    query = urllib.parse.quote("taskType=JSON_JQ_TRANSFORM")
    search = request("GET", f"/tasks/search?query={query}&start=0&size=10000&sort=startTime%3ADESC")
    evidence = {}
    names = {(key[0], task["name"]): key for key, task in local.items()}
    for hit in search.get("results") or []:
        name_key = (str(hit.get("workflowType") or ""), str(hit.get("taskDefName") or ""))
        key = names.get(name_key)
        if key is None or key in evidence:
            continue
        task = request("GET", f"/tasks/{hit['taskId']}")
        inputs = compact(task.get("inputData") or {})
        inputs.pop("queryExpression", None)
        evidence[key] = inputs
        if len(evidence) == len(local):
            break
    return evidence


def delete_proof_definition() -> None:
    try:
        request("GET", f"/metadata/workflow/{PROOF_NAME}?version=1")
    except RuntimeError as exc:
        if "HTTP 404" in str(exc) or "HTTP 500" in str(exc):
            return
        raise
    try:
        request("DELETE", f"/metadata/workflow/{PROOF_NAME}/1")
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise


def main() -> int:
    local = catalog()
    registered_expressions(local)
    recorded = recorded_inputs(local)
    examples = catalog_inputs()
    missing_examples = sorted(set(local) - set(recorded) - set(examples))
    if missing_examples:
        raise RuntimeError(f"no runtime or catalog evidence for: {missing_examples}")

    tasks = []
    sources = {}
    for index, key in enumerate(sorted(local), start=1):
        task = local[key]
        source = "recorded_runtime" if key in recorded else "catalog_example"
        inputs = recorded[key] if key in recorded else examples[key]
        reference = f"jq_contract_{index:02d}"
        tasks.append({
            "name": reference,
            "taskReferenceName": reference,
            "type": "JSON_JQ_TRANSFORM",
            "inputParameters": {
                **inputs,
                "queryExpression": task["inputParameters"]["queryExpression"],
            },
        })
        sources[reference] = {"workflow": key[0], "task": key[1], "source": source}

    definition = {
        "name": PROOF_NAME,
        "description": "Operational replay of every registered coding-harness JQ contract",
        "version": 1,
        "schemaVersion": 2,
        "ownerEmail": "conductor@localhost",
        "tasks": tasks,
        "outputParameters": {},
    }
    delete_proof_definition()
    request("POST", "/metadata/workflow/validate", definition)
    request("POST", "/metadata/workflow", definition)
    started = request("POST", f"/workflow/{PROOF_NAME}", {})
    workflow_id = started.get("workflowId") if isinstance(started, dict) else str(started)
    while True:
        execution = request("GET", f"/workflow/{workflow_id}?includeTasks=true")
        if execution["status"] in TERMINAL:
            break
        time.sleep(0.2)

    failures = []
    for task in execution.get("tasks") or []:
        if task.get("status") != "COMPLETED":
            failures.append({
                **sources.get(task.get("referenceTaskName"), {}),
                "status": task.get("status"),
                "reason": task.get("reasonForIncompletion"),
            })
    report = {
        "workflowId": workflow_id,
        "status": execution["status"],
        "registeredExpressionsExact": True,
        "catalogTasks": len(local),
        "completedTasks": len(tasks) - len(failures),
        "recordedRuntimeInputs": len(recorded),
        "catalogExampleInputs": len(local) - len(recorded),
        "unprovenProductionContracts": [
            {"workflow": workflow, "task": task}
            for workflow, task in sorted(set(local) - set(recorded))
        ],
        "failures": failures,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if execution["status"] == "COMPLETED" and not failures else 1


if __name__ == "__main__":
    sys.exit(main())
