#!/usr/bin/env python3
"""Prove the repaired address-PR verification contract with real tasks.

This is not a `/workflow/test` execution and contains no mocked task output.  A
real `test_run` worker emits its canonical verification envelope, and the real
`address_pr_repair` sub-workflow consumes the complete `${verify.output}` value.
The proof succeeds only when Conductor records exact JSON equality between the
producer output and the resolved sub-workflow input and the child completes.

The verification producer uses its legitimate `no_tests_required` outcome, so
the probe is read-only and does not invoke a coding agent or publication task.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKERS = ROOT / "workers"
sys.path.insert(0, str(WORKERS))

from common import git  # noqa: E402


BASE = os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080/api").rstrip("/")
TOKEN = os.environ.get("CONDUCTOR_AUTH_TOKEN", "")
PROOF_NAME = "coding_harness_address_repair_contract_proof"
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


def delete_definition() -> None:
    try:
        request("GET", f"/metadata/workflow/{PROOF_NAME}?version=1")
    except RuntimeError as exc:
        if "HTTP 404" in str(exc) or "HTTP 500" in str(exc):
            return
        raise
    request("DELETE", f"/metadata/workflow/{PROOF_NAME}/1")


def source_contract() -> dict:
    definition = json.loads(
        (ROOT / "workers" / "workflows" / "address_pr.json").read_text(encoding="utf-8")
    )
    for value in _walk(definition.get("tasks") or []):
        if value.get("taskReferenceName") == "repair":
            return value
    raise RuntimeError("address_pr repair task is missing")


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def main() -> int:
    source = source_contract()
    if source.get("type") != "SUB_WORKFLOW":
        raise RuntimeError("address_pr repair is not a SUB_WORKFLOW")
    if (source.get("subWorkflowParam") or {}) != {"name": "address_pr_repair", "version": 1}:
        raise RuntimeError("address_pr repair does not pin address_pr_repair version 1")
    if (source.get("inputParameters") or {}).get("verification") != "${verify.output}":
        raise RuntimeError("address_pr repair no longer consumes the full verify.output envelope")

    # Worker gate for the only SIMPLE task introduced by this proof workflow.
    task_definition = request("GET", "/metadata/taskdefs/test_run")
    if task_definition.get("name") != "test_run":
        raise RuntimeError("test_run task definition is not registered")

    head = git.git(str(ROOT), "rev-parse", "HEAD").stdout.strip()
    if not head:
        raise RuntimeError("unable to resolve the harness HEAD")
    definition = {
        "name": PROOF_NAME,
        "description": "Operational proof of test_run output to address_pr_repair input",
        "version": 1,
        "schemaVersion": 2,
        "ownerEmail": "conductor@localhost",
        "inputParameters": ["repoPath", "candidateCommit"],
        "tasks": [
            {
                "name": "test_run",
                "taskReferenceName": "verify",
                "type": "SIMPLE",
                "inputParameters": {
                    "candidateCommit": "${workflow.input.candidateCommit}",
                    "commands": [],
                    "discoveryOutcome": "no_tests_required",
                    "discoveryReason": "read-only operational task-contract proof",
                    "repoPath": "${workflow.input.repoPath}",
                },
            },
            {
                "name": "address_pr_repair",
                "taskReferenceName": "repair",
                "type": "SUB_WORKFLOW",
                "subWorkflowParam": {"name": "address_pr_repair", "version": 1},
                "inputParameters": {
                    "candidateCommit": "${workflow.input.candidateCommit}",
                    "fixPromptTemplate": "",
                    "fixPromptTemplateSource": "",
                    "maxBudgetUsd": 0,
                    "maxTurns": 1,
                    "model": "",
                    "agent": "",
                    "modelOverrides": {},
                    "modelPolicy": {},
                    "modelPolicySha256": "",
                    "modelPolicySource": "",
                    "modelProfile": "",
                    "modelsConfig": "",
                    "repoPath": "${workflow.input.repoPath}",
                    "verification": "${verify.output}",
                },
            },
        ],
        "outputParameters": {
            "producer": "${verify.output}",
            "consumer": "${repair.output}",
        },
    }
    delete_definition()
    request("POST", "/metadata/workflow/validate", definition)
    request("POST", "/metadata/workflow", definition)
    started = request("POST", f"/workflow/{PROOF_NAME}", {
        "repoPath": str(ROOT),
        "candidateCommit": head,
    })
    workflow_id = started.get("workflowId") if isinstance(started, dict) else str(started)
    while True:
        execution = request("GET", f"/workflow/{workflow_id}?includeTasks=true")
        if execution.get("status") in TERMINAL:
            break
        time.sleep(0.2)

    tasks = {task.get("referenceTaskName"): task for task in execution.get("tasks") or []}
    producer = tasks.get("verify") or {}
    consumer = tasks.get("repair") or {}
    resolved = ((consumer.get("inputData") or {}).get("workflowInput") or {}).get("verification")
    produced = producer.get("outputData")
    child_output = consumer.get("outputData") or {}
    address_repair_id = str(child_output.get("subWorkflowId") or "")
    address_repair = request(
        "GET", f"/workflow/{address_repair_id}?includeTasks=true"
    ) if address_repair_id else {}
    nested_repair = next(
        (task for task in (address_repair.get("tasks") or [])
         if task.get("referenceTaskName") == "repair"),
        {},
    )
    remediation_id = str((nested_repair.get("outputData") or {}).get("subWorkflowId") or "")
    remediation = request(
        "GET", f"/workflow/{remediation_id}?includeTasks=true"
    ) if remediation_id else {}
    required = next(
        (task for task in (remediation.get("tasks") or [])
         if task.get("referenceTaskName") == "required_verification"),
        {},
    )
    required_prior = (required.get("inputData") or {}).get("prior")
    normalized_commands = (required.get("outputData") or {}).get("result")
    checks = {
        "workflowCompleted": execution.get("status") == "COMPLETED",
        "producerCompleted": producer.get("status") == "COMPLETED",
        "producerCanonicalObject": (
            isinstance(produced, dict)
            and isinstance(produced.get("commands"), list)
            and produced.get("verificationState") == "passed"
        ),
        "exactResolvedInputEquality": produced == resolved,
        "consumerCompleted": consumer.get("status") == "COMPLETED",
        "childAcceptedEnvelope": (
            address_repair.get("status") == "COMPLETED"
            and remediation.get("status") == "COMPLETED"
            and required.get("status") == "COMPLETED"
            and required_prior == produced
            and normalized_commands == produced.get("commands")
        ),
        "workerGate": task_definition.get("name") == "test_run",
        "versionOne": definition["version"] == 1,
    }
    report = {
        "workflowId": workflow_id,
        "status": execution.get("status"),
        "producerTaskId": producer.get("taskId"),
        "consumerTaskId": consumer.get("taskId"),
        "addressRepairWorkflowId": address_repair_id,
        "verificationRemediationWorkflowId": remediation_id,
        "requiredVerificationTaskId": required.get("taskId"),
        "producerShape": {
            "type": type(produced).__name__,
            "keys": sorted(produced) if isinstance(produced, dict) else [],
            "commandsType": type(produced.get("commands")).__name__
            if isinstance(produced, dict) else "missing",
        },
        "resolvedConsumerShape": {
            "type": type(resolved).__name__,
            "keys": sorted(resolved) if isinstance(resolved, dict) else [],
            "commandsType": type(resolved.get("commands")).__name__
            if isinstance(resolved, dict) else "missing",
        },
        "checks": checks,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    # The execution remains inspectable, but the temporary definition must not
    # pollute the repository's exactly-version-1 workflow registry.
    delete_definition()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
