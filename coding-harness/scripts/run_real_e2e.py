#!/usr/bin/env python3
"""Run resumable, non-mocked end-to-end harness scenarios.

The runner creates real GitHub artifacts in a dedicated test repository and
starts ordinary Conductor executions (never ``/workflow/test``).  Each run is
materialized under ``reports/real-e2e/<scenario-id>`` so a stopped process can
resume without duplicating an issue, branch, workflow, or pull request.

The first phase drives ``feature_campaign`` through every checkpoint and leaves
its real PR ready for the review/fix phases.  Later phases use the same manifest
and evidence format; they are deliberately dependent on the preceding GitHub
state rather than being independent smoke tests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
WORKERS = ROOT / "workers"
sys.path.insert(0, str(WORKERS))

from common import git, github  # noqa: E402


DEFAULT_REPO = "conductor-oss/coding-agent-test"
DEFAULT_SERVER = "http://localhost:8080/api"
TERMINAL = {"COMPLETED", "FAILED", "TIMED_OUT", "TERMINATED"}
# Maps each feature_campaign WAIT gate to the SIMPLE task (worker: campaign_checkpoint,
# see workers/campaign/tasks.py + workers/campaign/model.py::validate_checkpoint) that
# validates the signaled decision against that phase's own blocking status. That
# validation -- and the fail-closed revision-limit switches downstream of it -- is the
# real safety net now; it lives in the workflow, not in this driver. This map exists
# purely so the driver can look up and log the decision's {valid, action, feedback}
# after signaling, for evidence -- it is never used to gate whether to signal.
CAMPAIGN_CHECKPOINTS = {
    "design_checkpoint": "design_decision",
    "plan_checkpoint": "plan_decision",
    "wave_checkpoint": "wave_decision",
    "final_checkpoint": "final_decision",
}


def workflow_simple_tasks(workflow: str) -> list[str]:
    definition = json.loads(
        (ROOT / "workers" / "workflows" / f"{workflow}.json").read_text(encoding="utf-8")
    )
    names: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "SIMPLE" and isinstance(value.get("name"), str):
                names.add(value["name"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(definition.get("tasks") or [])
    return sorted(names)


def worker_gate(conductor: "Conductor", workflow: str) -> list[str]:
    names = workflow_simple_tasks(workflow)
    for name in names:
        conductor.task_definition(name)
    return names


CASES: tuple[dict[str, Any], ...] = (
    {
        "slug": "parcel-event-reconciler",
        "title": "Build a deterministic parcel event reconciler",
        "entity": "parcel",
        "language": "python",
        "states": ["created", "accepted", "in_transit", "delivered", "cancelled"],
        "invariants": [
            "Duplicate event IDs are idempotent only when their complete payloads match; conflicting duplicates are errors.",
            "Order events by parsed UTC instant, then event ID, regardless of their input order or timezone offset spelling.",
            "Delivery is terminal: a later non-delivery event must be rejected without partially mutating state.",
        ],
    },
    {
        "slug": "quota-window-ledger",
        "title": "Build a deterministic quota window ledger",
        "entity": "account",
        "language": "python",
        "states": ["opened", "credited", "debited", "frozen", "closed"],
        "invariants": [
            "A repeated operation ID is idempotent only for an identical amount and account; mismatches are errors.",
            "Events at the same instant are ordered by operation ID and window boundaries are inclusive at start and exclusive at end.",
            "A debit may never make the balance negative, and a rejected debit must not alter later report totals.",
        ],
    },
    {
        "slug": "release-dependency-journal",
        "title": "Build a deterministic release dependency journal",
        "entity": "release",
        "language": "python",
        "states": ["planned", "ready", "started", "completed", "rolled_back"],
        "invariants": [
            "Dependency cycles must report the complete lexicographically smallest cycle rather than a generic failure.",
            "Independent ready releases are emitted in stable lexical order even when input maps have different insertion order.",
            "Rollback is terminal for that attempt but does not complete dependents, and repeated identical records are idempotent.",
        ],
    },
    {
        "slug": "sensor-sample-compactor",
        "title": "Build a deterministic sensor sample compactor",
        "entity": "sensor",
        "language": "python",
        "states": ["online", "sampled", "warning", "offline", "retired"],
        "invariants": [
            "Samples exactly on a bucket boundary belong to the later bucket; timestamps are normalized to UTC.",
            "Equal-timestamp samples use sample ID as a tie-break and conflicting duplicate IDs are errors.",
            "NaN, infinity, booleans, and numeric strings are rejected as measurements without changing aggregates.",
        ],
    },
    {
        "slug": "reservation-state-ledger",
        "title": "Build a deterministic reservation state ledger",
        "entity": "reservation",
        "language": "python",
        "states": ["requested", "held", "confirmed", "released", "expired"],
        "invariants": [
            "Expiration at the exact deadline wins over confirmation at that same instant.",
            "Conflicting duplicate command IDs are errors while exact duplicates are idempotent.",
            "Rejected transitions are included in diagnostics but never in successful transition counts.",
        ],
    },
    {
        "slug": "warehouse-slot-allocator",
        "title": "Build a deterministic warehouse slot allocator",
        "entity": "slot",
        "language": "java",
        "states": ["reserved", "allocated", "picking", "shipped", "released"],
        "invariants": [
            "A repeated allocation ID is idempotent only when its slot, quantity, and destination match exactly; a conflicting duplicate is an error.",
            "Order events by parsed UTC instant, then allocation ID, regardless of their input order or timezone offset spelling.",
            "Shipped is terminal: a later non-released event for that slot must be rejected without partially mutating state.",
        ],
    },
    {
        "slug": "meter-billing-cycle",
        "title": "Build a deterministic meter billing cycle tracker",
        "entity": "meter",
        "language": "csharp",
        "states": ["registered", "reading_open", "reading_closed", "billed", "disputed"],
        "invariants": [
            "A repeated reading ID is idempotent only when its meter and value match exactly; a conflicting duplicate is an error.",
            "Order events by parsed UTC instant, then reading ID, regardless of their input order or timezone offset spelling.",
            "Billed is terminal except for a dispute: any other later event for that meter must be rejected without partially mutating state.",
        ],
    },
    {
        "slug": "cluster-node-lifecycle",
        "title": "Build a deterministic cluster node lifecycle tracker",
        "entity": "node",
        "language": "go",
        "states": ["joining", "ready", "draining", "cordoned", "removed"],
        "invariants": [
            "A repeated transition ID is idempotent only when its node and target state match exactly; a conflicting duplicate is an error.",
            "Order events by parsed UTC instant, then node ID, regardless of their input order or timezone offset spelling.",
            "Removed is terminal: a later non-removed event for that node must be rejected without partially mutating state.",
        ],
    },
    {
        "slug": "webhook-delivery-tracker",
        "title": "Build a deterministic webhook delivery tracker",
        "entity": "delivery",
        "language": "typescript",
        "states": ["queued", "sending", "delivered", "retrying", "dead_lettered"],
        "invariants": [
            "A repeated delivery attempt ID is idempotent only when its endpoint and payload hash match exactly; a conflicting duplicate is an error.",
            "Order events by parsed UTC instant, then attempt ID, regardless of their input order or timezone offset spelling.",
            "Delivered and dead_lettered are both terminal: a later event for that delivery must be rejected without partially mutating state.",
        ],
    },
    {
        "slug": "incident-timeline-board",
        "title": "Build a deterministic incident timeline board",
        "entity": "incident",
        "language": "typescript-react",
        "states": ["reported", "acknowledged", "mitigating", "resolved", "reopened"],
        "invariants": [
            "A repeated update ID is idempotent only when its incident and severity match exactly; a conflicting duplicate is an error.",
            "Order events by parsed UTC instant, then update ID, regardless of their input order or timezone offset spelling.",
            "Resolved is terminal except for a reopen: any other later event for that incident must be rejected without partially mutating state.",
        ],
    },
    {
        "slug": "ledger-settlement-batch",
        "title": "Build a deterministic ledger settlement batch processor",
        "entity": "settlement",
        "language": "rust",
        "states": ["pending", "matched", "settled", "failed", "reversed"],
        "invariants": [
            "A repeated settlement ID is idempotent only when its account and amount match exactly; a conflicting duplicate is an error.",
            "Order events by parsed UTC instant, then settlement ID, regardless of their input order or timezone offset spelling.",
            "Settled is terminal except for a reversal: any other later event for that settlement must be rejected without partially mutating state.",
        ],
    },
    {
        "slug": "device-provisioning-ledger",
        "title": "Build a deterministic device provisioning ledger",
        "entity": "device",
        "language": "cpp",
        "states": ["unprovisioned", "provisioning", "active", "quarantined", "decommissioned"],
        "invariants": [
            "A repeated provisioning event ID is idempotent only when its device and firmware version match exactly; a conflicting duplicate is an error.",
            "Order events by parsed UTC instant, then device ID, regardless of their input order or timezone offset spelling.",
            "Decommissioned is terminal: a later non-decommissioned event for that device must be rejected without partially mutating state.",
        ],
    },
)

LANGUAGES: tuple[str, ...] = tuple(sorted({case["language"] for case in CASES}))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Conductor:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.token = os.environ.get("CONDUCTOR_AUTH_TOKEN", "")

    def request(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Authorization"] = self.token
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request) as response:  # noqa: S310
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"{method} {path} returned HTTP {exc.code}: {detail[:1000]}"
            ) from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def start(self, workflow: str, payload: dict[str, Any]) -> str:
        result = self.request("POST", f"/workflow/{workflow}", payload)
        workflow_id = result.get("workflowId") if isinstance(result, dict) else result
        value = str(workflow_id or "").strip().strip('"')
        if not value:
            raise RuntimeError(f"Conductor did not return an ID for {workflow}")
        return value

    def execution(self, workflow_id: str) -> dict[str, Any]:
        value = self.request("GET", f"/workflow/{workflow_id}?includeTasks=true")
        if not isinstance(value, dict):
            raise RuntimeError(f"invalid execution response for {workflow_id}")
        return value

    def signal(self, workflow_id: str, task_ref: str, output: dict[str, Any]) -> None:
        result = self.request(
            "POST", f"/tasks/{workflow_id}/{task_ref}/COMPLETED/sync", output
        )
        if result is None:
            raise RuntimeError(f"signal {task_ref} returned no execution")

    def task_definition(self, name: str) -> dict[str, Any]:
        value = self.request("GET", f"/metadata/taskdefs/{name}")
        if not isinstance(value, dict) or value.get("name") != name:
            raise RuntimeError(f"required SIMPLE task definition {name!r} is unavailable")
        return value


class Scenario:
    def __init__(self, run_dir: Path, conductor: Conductor):
        self.run_dir = run_dir
        self.state_path = run_dir / "state.json"
        self.conductor = conductor
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def save(self) -> None:
        self.state["updatedAt"] = utc_now()
        atomic_json(self.state_path, self.state)

    def event(self, kind: str, **fields: Any) -> None:
        entry = {"at": utc_now(), "kind": kind, **fields}
        self.state.setdefault("events", []).append(entry)
        self.save()
        print(json.dumps(entry, sort_keys=True), flush=True)

    def snapshot(self, execution: dict[str, Any]) -> None:
        workflow_id = str(execution.get("workflowId") or self.state.get("campaignWorkflowId"))
        atomic_json(self.run_dir / "executions" / f"{workflow_id}.json", execution)


def prior_case_counts(root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in root.glob("*/state.json"):
        try:
            slug = str(json.loads(path.read_text()).get("case", {}).get("slug") or "")
        except (OSError, ValueError):
            continue
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
    return counts


def choose_case(root: Path, language: str | None = None) -> dict[str, Any]:
    counts = prior_case_counts(root)
    pool = [case for case in CASES if language is None or case["language"] == language]
    if not pool:
        raise SystemExit(f"no scenario is defined for language {language!r}; known languages: {', '.join(LANGUAGES)}")
    selected = min(pool, key=lambda case: (counts.get(case["slug"], 0), case["slug"]))
    return json.loads(json.dumps(selected))


def _shared_invariants(case: dict[str, Any]) -> list[str]:
    """Three invariants every scenario carries in addition to its own, so every
    language proves the same base behavior: deterministic error reporting,
    reproducible serialization, and a well-formed empty-input result."""
    return list(case["invariants"]) + [
        "Input validation errors must be deterministic and identify the zero-based input line.",
        "JSON output must use sorted keys and compact separators so byte output is reproducible.",
        "An empty stream returns a valid empty report, not an exception.",
    ]


def _invariants_block(case: dict[str, Any]) -> str:
    return "\n".join(f"- {item}" for item in _shared_invariants(case))


def _states_block(case: dict[str, Any]) -> str:
    return ", ".join(f"`{item}`" for item in case["states"])


def _pkg(scenario_id: str) -> str:
    """Every language variant lives under its own scenario-derived directory
    (``features/<pkg>``) so concurrent/repeated runs against the same
    long-lived test repository never collide."""
    return scenario_id.replace("-", "_")


def issue_body(case: dict[str, Any], scenario_id: str) -> str:
    builder = _ISSUE_BODY_BUILDERS.get(case.get("language", "python"))
    if builder is None:
        raise ValueError(f"no issue-body builder registered for language {case.get('language')!r}")
    return builder(case, scenario_id)


def _issue_body_python(case: dict[str, Any], scenario_id: str) -> str:
    package = _pkg(scenario_id)
    directory = f"features/{package}"
    example_id = scenario_id[-8:]
    invariants = _invariants_block(case)
    states = _states_block(case)
    return f"""Build a self-contained Python 3 standard-library feature under `{directory}`.

The feature reconciles newline-delimited JSON events for a {case['entity']} into a deterministic state and report. Supported states are {states}. It must expose an importable library and `python3 -m features.{package}.cli` with stdin/stdout operation. Do not modify files outside `{directory}` and `docs/{package}.md`.

Required exact files:
- `{directory}/__init__.py`
- `{directory}/model.py`
- `{directory}/reconcile.py`
- `{directory}/report.py`
- `{directory}/cli.py`
- `{directory}/tests/test_reconcile.py`
- `{directory}/tests/test_cli.py`
- `docs/{package}.md`

Behavioral invariants:
{invariants}

The tests must include shuffled input order, offset-equivalent timestamps, exact and conflicting duplicates, terminal-state transitions, malformed input, and the unique example identifier `{example_id}`. Run them with:

`python3 -m unittest discover -s {directory}/tests -v`

Keep implementation modules independently understandable, use no third-party packages, and document the input/output schema plus state transition table.
"""


def _issue_body_java(case: dict[str, Any], scenario_id: str) -> str:
    pkg = _pkg(scenario_id)
    directory = f"features/{pkg}"
    java_pkg = f"features.{pkg}"
    example_id = scenario_id[-8:]
    invariants = _invariants_block(case)
    states = _states_block(case)
    return f"""Build a self-contained Java 17 feature under `{directory}`, as its own independent Maven project (its own `pom.xml` — do not add it as a module of any other build).

The feature reconciles newline-delimited JSON events for a {case['entity']} into a deterministic state and report. Supported states are {states}. It must expose an importable library (package `{java_pkg}`) and a runnable CLI (`{java_pkg}.Cli`, runnable as `java -cp target/classes {java_pkg}.Cli` after `mvn -q -f {directory}/pom.xml compile`) that reads events from stdin and writes the report to stdout. Do not modify files outside `{directory}` and `docs/{pkg}.md`.

Required exact files:
- `{directory}/pom.xml` (groupId `features`, artifactId `{pkg}`, packaging `jar`, Java 17 source/target compatibility, a `maven-surefire-plugin` bound to the `test` phase, and a single test-scope dependency on `org.junit.jupiter:junit-jupiter:5.10.2`)
- `{directory}/src/main/java/features/{pkg}/EventRecord.java`
- `{directory}/src/main/java/features/{pkg}/Model.java`
- `{directory}/src/main/java/features/{pkg}/Reconciler.java`
- `{directory}/src/main/java/features/{pkg}/Report.java`
- `{directory}/src/main/java/features/{pkg}/Cli.java`
- `{directory}/src/test/java/features/{pkg}/ReconcilerTest.java`
- `{directory}/src/test/java/features/{pkg}/CliTest.java`
- `docs/{pkg}.md`

Behavioral invariants:
{invariants}

The tests must include shuffled input order, offset-equivalent timestamps, exact and conflicting duplicates, terminal-state transitions, malformed input, and the unique example identifier `{example_id}`. `CliTest` must exercise the real stdin/stdout contract (via `ProcessBuilder` against the compiled classes, or by calling an exposed `Cli.run(InputStream, PrintStream)` that `main` itself delegates to). Run them with:

`mvn -q -f {directory}/pom.xml test`

Keep implementation classes independently understandable, use only the JDK standard library plus JUnit 5 in test scope (no other dependency in any scope), and document the input/output schema plus state transition table in `docs/{pkg}.md`.
"""


def _issue_body_csharp(case: dict[str, Any], scenario_id: str) -> str:
    pkg = _pkg(scenario_id)
    directory = f"features/{pkg}"
    proj = "".join(part.capitalize() for part in pkg.split("_"))
    ns = f"Features.{proj}"
    example_id = scenario_id[-8:]
    invariants = _invariants_block(case)
    states = _states_block(case)
    return f"""Build a self-contained .NET 8 feature under `{directory}`, as two independent projects (no shared root project or solution, and don't touch any other project in this repository).

The feature reconciles newline-delimited JSON events for a {case['entity']} into a deterministic state and report. Supported states are {states}. It must expose an importable class library (namespace `{ns}`) and a runnable CLI (`{ns}.Program`, runnable as `dotnet run --project {directory}/src/{proj}.csproj`) that reads events from stdin and writes the report to stdout. Do not modify files outside `{directory}` and `docs/{pkg}.md`.

The main project and its test project MUST be sibling directories (`{directory}/src/` and `{directory}/tests/`), never one nested inside the other: an SDK-style `.csproj`'s default item glob is `**/*.cs` relative to its own directory, so a test project placed under the main project's directory gets its `.cs` files compiled into *both* projects — the main project then fails with `CS0246: The type or namespace name 'TestMethod' could not be found`, since it has no MSTest reference. Sibling directories avoid this entirely.

Required exact files:
- `{directory}/src/{proj}.csproj` (SDK `Microsoft.NET.Sdk`, `TargetFramework` `net8.0`, `OutputType` `Exe`)
- `{directory}/src/Program.cs`
- `{directory}/src/Model.cs`
- `{directory}/src/Reconciler.cs`
- `{directory}/src/Report.cs`
- `{directory}/tests/{proj}.Tests.csproj` (SDK `Microsoft.NET.Sdk`, `TargetFramework` `net8.0`, `IsPackable` false, package references only to `Microsoft.NET.Test.Sdk`, `MSTest.TestAdapter`, and `MSTest.TestFramework`, plus a `ProjectReference` to `../src/{proj}.csproj`)
- `{directory}/tests/ReconcilerTests.cs`
- `{directory}/tests/CliTests.cs`
- `docs/{pkg}.md`

Behavioral invariants:
{invariants}

The tests must include shuffled input order, offset-equivalent timestamps, exact and conflicting duplicates, terminal-state transitions, malformed input, and the unique example identifier `{example_id}`. `CliTests` must exercise the actual stdin/stdout contract of `Program.Main`. Run them with:

`dotnet test {directory}/tests/{proj}.Tests.csproj`

Keep implementation classes independently understandable, use only the .NET 8 base class library plus the first-party MSTest packages in the test project (no other NuGet package in either project), and document the input/output schema plus state transition table in `docs/{pkg}.md`.
"""


def _issue_body_go(case: dict[str, Any], scenario_id: str) -> str:
    pkg = _pkg(scenario_id)
    directory = f"features/{pkg}"
    go_pkg = pkg.replace("_", "")
    example_id = scenario_id[-8:]
    invariants = _invariants_block(case)
    states = _states_block(case)
    return f"""Build a self-contained Go feature under `{directory}`, as its own independent Go module (its own `go.mod` — do not add it to any other module).

The feature reconciles newline-delimited JSON events for a {case['entity']} into a deterministic state and report. Supported states are {states}. It must expose an importable package (`package {go_pkg}`) with a `Run(io.Reader, io.Writer) error` entry point, plus a thin `cmd/cli/main.go` that calls `Run` against `os.Stdin`/`os.Stdout`. Do not modify files outside `{directory}` and `docs/{pkg}.md`.

Required exact files:
- `{directory}/go.mod` (`module {pkg}`, `go 1.22`, no `require` block)
- `{directory}/model.go`
- `{directory}/reconcile.go`
- `{directory}/report.go`
- `{directory}/cmd/cli/main.go`
- `{directory}/reconcile_test.go`
- `{directory}/cli_test.go` (must exercise `Run` through an `io.Reader`/`io.Writer` pair the same way `main` does — the real stdin/stdout contract, not a reimplementation)
- `docs/{pkg}.md`

Behavioral invariants:
{invariants}

The tests must include shuffled input order, offset-equivalent timestamps, exact and conflicting duplicates, terminal-state transitions, malformed input, and the unique example identifier `{example_id}`. Run them with:

`go test ./{directory}/...`

Keep implementation files independently understandable, use only the Go standard library (no entries in a `require` block), and document the input/output schema plus state transition table in `docs/{pkg}.md`.
"""


def _issue_body_typescript(case: dict[str, Any], scenario_id: str) -> str:
    pkg = _pkg(scenario_id)
    directory = f"features/{pkg}"
    example_id = scenario_id[-8:]
    invariants = _invariants_block(case)
    states = _states_block(case)
    return f"""Build a self-contained TypeScript (Node.js) feature under `{directory}`, as its own independent npm package (its own `package.json` — do not add it as a workspace member of any other package).

The feature reconciles newline-delimited JSON events for a {case['entity']} into a deterministic state and report. Supported states are {states}. It must expose an importable module and a runnable CLI (`{directory}/src/cli.ts`, compiled to `dist/cli.js`) that reads events from stdin and writes the report to stdout. Do not modify files outside `{directory}` and `docs/{pkg}.md`.

Required exact files:
- `{directory}/package.json` (name `@features/{pkg}`, `"type": "module"`, `devDependencies` containing only `typescript`, and scripts `"build": "tsc -p tsconfig.json"` plus `"test": "npm run build && node --test dist/"`)
- `{directory}/tsconfig.json` (`"outDir": "dist"`, `"rootDir": "src"`, target `ES2022`, `"strict": true`)
- `{directory}/src/model.ts`
- `{directory}/src/reconcile.ts`
- `{directory}/src/report.ts`
- `{directory}/src/cli.ts`
- `{directory}/src/reconcile.test.ts` (using Node's built-in `node:test` and `node:assert/strict`)
- `{directory}/src/cli.test.ts` (must exercise the compiled CLI's real stdin/stdout contract, e.g. via `node:child_process`)
- `docs/{pkg}.md`

Behavioral invariants:
{invariants}

The tests must include shuffled input order, offset-equivalent timestamps, exact and conflicting duplicates, terminal-state transitions, malformed input, and the unique example identifier `{example_id}`. Run them with:

`npm --prefix {directory} test`

Keep implementation modules independently understandable, use only TypeScript itself (as a devDependency, to compile) plus Node's built-in `node:test`/`node:assert` test runner — no other dependency or devDependency — and document the input/output schema plus state transition table in `docs/{pkg}.md`.
"""


def _issue_body_typescript_react(case: dict[str, Any], scenario_id: str) -> str:
    pkg = _pkg(scenario_id)
    directory = f"features/{pkg}"
    example_id = scenario_id[-8:]
    invariants = _invariants_block(case)
    states = _states_block(case)
    return f"""Build a self-contained TypeScript + React single-page app under `{directory}`, as its own independent Vite project (its own `package.json` — do not add it as a workspace member of any other package). This is a genuine interactive UI, not a headless library: a person pastes newline-delimited JSON events into it and watches the reconciled result render live.

The feature reconciles newline-delimited JSON events for a {case['entity']} into a deterministic state and report. Supported states are {states}. The app must offer a textarea for pasting events and a "Reconcile" action that renders: a table of every {case['entity']}'s current state sorted deterministically (by ID), a summary count per state, and a list of rejected/malformed lines with their zero-based line numbers. The reconciliation algorithm itself (`src/reconcile.ts`) must be original, dependency-free logic — it may import only `src/model.ts`, nothing else. Do not modify files outside `{directory}` and `docs/{pkg}.md`.

Required exact files:
- `{directory}/package.json` (`"type": "module"`; `dependencies` limited to `react` and `react-dom`; `devDependencies` limited to `vite`, `@vitejs/plugin-react`, `typescript`, `vitest`, `@testing-library/react`, `@testing-library/jest-dom`, `jsdom`; scripts `"build": "vite build"` and `"test": "vitest run"`)
- `{directory}/vite.config.ts` (React plugin, plus a `test` block configuring the `jsdom` environment and a setup file)
- `{directory}/tsconfig.json`
- `{directory}/index.html`
- `{directory}/src/main.tsx`
- `{directory}/src/model.ts`
- `{directory}/src/reconcile.ts`
- `{directory}/src/App.tsx`
- `{directory}/src/setupTests.ts` — must read exactly:
  ```ts
  import {{ afterEach, expect }} from 'vitest';
  import {{ cleanup }} from '@testing-library/react';
  import * as matchers from '@testing-library/jest-dom/matchers';

  expect.extend(matchers);
  afterEach(cleanup);
  ```
  Two confirmed-live failure modes this avoids: (1) the bare `import '@testing-library/jest-dom';` side-effect form assumes a Jest-style global `expect`, which Vitest does not provide, failing every test with `ReferenceError: expect is not defined`; (2) without an explicit `afterEach(cleanup)`, nothing unmounts the component between tests (Testing Library's auto-cleanup needs a global `afterEach`, which this project's `vite.config.ts` intentionally does not enable), so a second test's `render()` leaves the first test's DOM in place too — a query like `getByLabelText('events')` that matched one element in isolation then throws "multiple elements found" the moment a second test runs.
- `{directory}/src/reconcile.test.ts` (pure-logic unit tests, no rendering)
- `{directory}/src/App.test.tsx` (component tests via React Testing Library: paste sample events, trigger reconciliation, assert the rendered rows, summary counts, and the rejected-line list; must cover a duplicate-conflict and a rejected terminal-state transition rendering as errors)
- `docs/{pkg}.md`

Behavioral invariants:
{invariants}

The tests must include shuffled input order, offset-equivalent timestamps, exact and conflicting duplicates, terminal-state transitions, malformed input, and the unique example identifier `{example_id}`. Run them with:

`npm --prefix {directory} test`

Keep `reconcile.ts` and `App.tsx` independently understandable, use no runtime dependency beyond `react`/`react-dom` and no devDependency beyond the Vite/Vitest/Testing-Library set listed above (no state-management or utility library), and document the input/output schema plus state transition table in `docs/{pkg}.md`.
"""


def _issue_body_rust(case: dict[str, Any], scenario_id: str) -> str:
    pkg = _pkg(scenario_id)
    directory = f"features/{pkg}"
    example_id = scenario_id[-8:]
    invariants = _invariants_block(case)
    states = _states_block(case)
    return f"""Build a self-contained Rust feature under `{directory}`, as its own independent Cargo package (its own `Cargo.toml` — do not add it to any workspace).

The feature reconciles newline-delimited JSON events for a {case['entity']} into a deterministic state and report. Supported states are {states}. It must expose a library crate and a `cli` binary (`src/bin/cli.rs`) that reads events from stdin and writes the report to stdout. Do not modify files outside `{directory}` and `docs/{pkg}.md`.

Required exact files:
- `{directory}/Cargo.toml` (package name `{pkg}`, `edition = "2021"`, an empty `[dependencies]` table, and `[[bin]]` name `cli` path `src/bin/cli.rs`)
- `{directory}/src/lib.rs`
- `{directory}/src/model.rs`
- `{directory}/src/reconcile.rs`
- `{directory}/src/report.rs`
- `{directory}/src/bin/cli.rs`
- `{directory}/tests/reconcile.rs` (an integration test using Rust's built-in test harness)
- `{directory}/tests/cli.rs` (spawns the built binary via `std::process::Command::new(env!("CARGO_BIN_EXE_cli"))` — the real stdin/stdout contract)
- `docs/{pkg}.md`

Behavioral invariants:
{invariants}

The tests must include shuffled input order, offset-equivalent timestamps, exact and conflicting duplicates, terminal-state transitions, malformed input, and the unique example identifier `{example_id}`. Run them with:

`cargo test --manifest-path {directory}/Cargo.toml`

Keep implementation modules independently understandable, use only the Rust standard library — an empty `[dependencies]` table and no `[dev-dependencies]` either — and document the input/output schema plus state transition table in `docs/{pkg}.md`.
"""


def _issue_body_cpp(case: dict[str, Any], scenario_id: str) -> str:
    pkg = _pkg(scenario_id)
    directory = f"features/{pkg}"
    example_id = scenario_id[-8:]
    invariants = _invariants_block(case)
    states = _states_block(case)
    return f"""Build a self-contained C++20 feature under `{directory}`, as its own independent CMake project (its own `CMakeLists.txt` — do not add it to any other build). Do not modify files outside `{directory}` and `docs/{pkg}.md`.

The feature reconciles newline-delimited JSON events for a {case['entity']} into a deterministic state and report. Supported states are {states}. It must expose a small static library plus a `cli` executable (`src/cli.cpp`) that reads events from `std::cin` and writes the report to `std::cout`.

Required exact files:
- `{directory}/CMakeLists.txt` (`cmake_minimum_required(VERSION 3.20)`, C++20, a library target for `model`/`reconcile`/`report`, a `cli` executable linking it, `enable_testing()`, a `{pkg}_tests` executable linking the library, and `add_test(NAME {pkg}_tests COMMAND {pkg}_tests)`)
- `{directory}/src/model.hpp`
- `{directory}/src/reconcile.hpp` and `{directory}/src/reconcile.cpp`
- `{directory}/src/report.hpp` and `{directory}/src/report.cpp`
- `{directory}/src/cli.cpp`
- `{directory}/tests/test_reconcile.cpp` (a hand-rolled `<cassert>`-based harness with a `main()` that returns non-zero on any failed check — no GoogleTest, Catch2, or any other third-party test framework)
- `{directory}/tests/test_cli.cpp` (exercises the real CLI code path — either by linking the library and calling an exposed `runCli(std::istream&, std::ostream&)` that `cli.cpp`'s `main` itself delegates to, or by invoking the built `cli` binary as a subprocess)
- `docs/{pkg}.md`

Behavioral invariants:
{invariants}

The tests must include shuffled input order, offset-equivalent timestamps, exact and conflicting duplicates, terminal-state transitions, malformed input, and the unique example identifier `{example_id}`. Run them with:

`cmake -S {directory} -B {directory}/build && cmake --build {directory}/build && ctest --test-dir {directory}/build --output-on-failure`

Keep implementation files independently understandable, use only the C++ standard library (no GoogleTest, Catch2, Boost, or any other third-party library), and document the input/output schema plus state transition table in `docs/{pkg}.md`.
"""


_ISSUE_BODY_BUILDERS: dict[str, Any] = {
    "python": _issue_body_python,
    "java": _issue_body_java,
    "csharp": _issue_body_csharp,
    "go": _issue_body_go,
    "typescript": _issue_body_typescript,
    "typescript-react": _issue_body_typescript_react,
    "rust": _issue_body_rust,
    "cpp": _issue_body_cpp,
}


def create_issue(repo: str, title: str, body: str) -> dict[str, Any]:
    github.ensure_git_auth()
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"title": title, "body": body}, handle)
        result = github.run(
            ["gh", "api", f"repos/{github.repo_slug(repo)}/issues", "--method", "POST", "--input", path]
        )
        value = json.loads(result.stdout or "{}")
    finally:
        os.unlink(path)
    if not isinstance(value, dict) or not value.get("number"):
        raise RuntimeError("GitHub did not return a created issue")
    return {"number": value["number"], "url": value.get("html_url"), "title": title, "body": body}


def task_ref(task: dict[str, Any]) -> str:
    return str(task.get("referenceTaskName") or "")


def latest_task(tasks: list[dict[str, Any]], base_ref: str) -> dict[str, Any] | None:
    matches = [task for task in tasks if task_ref(task) == base_ref or task_ref(task).startswith(base_ref + "__")]
    return matches[-1] if matches else None


def open_campaign_gate(execution: dict[str, Any]) -> dict[str, Any] | None:
    tasks = execution.get("tasks") or []
    for base_ref in CAMPAIGN_CHECKPOINTS:
        gate = latest_task(tasks, base_ref)
        if gate and gate.get("status") == "IN_PROGRESS" and gate.get("taskType") == "WAIT":
            return gate
    return None


def drive_campaign(scenario: Scenario) -> dict[str, Any]:
    workflow_id = str(scenario.state["campaignWorkflowId"])
    signaled_ids = set(scenario.state.setdefault("signaledTaskIds", []))
    while True:
        execution = scenario.conductor.execution(workflow_id)
        scenario.snapshot(execution)
        status = str(execution.get("status") or "UNKNOWN")
        scenario.state["campaignStatus"] = status
        scenario.save()
        if status in TERMINAL:
            scenario.event("campaign_terminal", workflowId=workflow_id, status=status)
            return execution

        gate = open_campaign_gate(execution)
        if gate:
            gate_id = str(gate.get("taskId") or "")
            if gate_id in signaled_ids:
                time.sleep(1)
                continue
            base_ref = task_ref(gate).split("__", 1)[0]
            draft = (gate.get("inputData") or {}).get("draft") or {}
            owner = str(gate.get("workflowInstanceId") or workflow_id)
            output = {"action": "continue", "feedback": "", "maxTurns": None, "maxBudgetUsd": None}
            scenario.conductor.signal(owner, task_ref(gate), output)
            signaled_ids.add(gate_id)
            scenario.state["signaledTaskIds"] = sorted(signaled_ids)
            # The real safety net (validate_checkpoint, workers/campaign/model.py) runs
            # server-side, downstream of this signal, and the workflow's own revision-limit
            # switches act on its verdict -- this driver no longer duplicates that decision.
            # Best-effort: log the decision task's verdict for evidence if it has already run;
            # a decision that hasn't executed yet by this next fetch just logs as unknown, it
            # is not treated as a failure.
            decision_ref = CAMPAIGN_CHECKPOINTS[base_ref]
            after = scenario.conductor.execution(workflow_id)
            decision = latest_task(after.get("tasks") or [], decision_ref)
            verdict = (decision.get("outputData") or {}) if decision else {}
            scenario.event(
                "checkpoint_advanced",
                gate=task_ref(gate),
                gateTaskId=gate_id,
                decisionTask=task_ref(decision or {}),
                valid=verdict.get("valid"),
                action=verdict.get("action"),
                feedback=verdict.get("feedback"),
                outcome=verdict.get("outcome"),
                draft=draft,
            )
            continue
        time.sleep(2)


def open_gate(execution: dict[str, Any], base_ref: str) -> dict[str, Any] | None:
    gate = latest_task(execution.get("tasks") or [], base_ref)
    if gate and gate.get("status") == "IN_PROGRESS" and gate.get("taskType") == "WAIT":
        return gate
    return None


def drive_review(scenario: Scenario, workflow_id: str) -> dict[str, Any]:
    signaled_ids = set(scenario.state.setdefault("signaledTaskIds", []))
    investigated = False
    while True:
        execution = scenario.conductor.execution(workflow_id)
        scenario.snapshot(execution)
        status = str(execution.get("status") or "UNKNOWN")
        if status in TERMINAL:
            scenario.event("review_terminal", workflowId=workflow_id, status=status)
            return execution
        gate = open_gate(execution, "review_gate")
        if gate:
            gate_id = str(gate.get("taskId") or "")
            if gate_id in signaled_ids:
                time.sleep(1)
                continue
            owner = str(gate.get("workflowInstanceId") or workflow_id)
            draft = (gate.get("inputData") or {}).get("draft") or {}
            scenario.state.setdefault("reviewDrafts", []).append({
                "taskId": gate_id,
                "taskRef": task_ref(gate),
                "draft": draft,
            })
            if not investigated and draft.get("canInvestigate") is True:
                case = scenario.state.get("case") or {}
                focus = " ".join(case.get("invariants") or [])
                output = {
                    "approved": False,
                    "action": "investigate",
                    "feedback": (
                        "Trace each seeded invariant through parsing, ordering, state mutation, "
                        "reporting, and CLI error handling. Look for an input where a rejected or "
                        f"duplicate event still changes observable state. Invariants: {focus}"
                    ),
                }
                action = "investigate"
                investigated = True
            else:
                # The GitHub identity running this E2E owns the PR and GitHub rejects self-approval
                # and self-request-changes. Preserve the real draft as evidence, then suppress only
                # publication; later phases can turn a concrete finding into an issue/comment.
                output = {
                    "approved": False,
                    "action": "stop",
                    "suppressed": True,
                    "feedback": "",
                }
                action = "stop"
            scenario.conductor.signal(owner, task_ref(gate), output)
            signaled_ids.add(gate_id)
            scenario.state["signaledTaskIds"] = sorted(signaled_ids)
            scenario.event(
                "review_gate_advanced",
                workflowId=workflow_id,
                gate=task_ref(gate),
                gateTaskId=gate_id,
                action=action,
                verdict=draft.get("verdict"),
                commentCount=len(draft.get("comments") or []),
            )
            continue
        time.sleep(2)


def create_campaign(args: argparse.Namespace, conductor: Conductor) -> Scenario:
    root = Path(args.reports).resolve()
    # Not "e2e-": that substring is one of verification.py's _HEAVY_MARKERS
    # (docker/integration/e2e/etc., meant to catch a genuinely heavy suite an
    # agent might try to sneak past discovery). A compiled-language scenario's
    # own inferred command has to embed its scenario-derived directory in argv
    # (go's "./features/<pkg>/...", cargo's "--manifest-path <pkg>/Cargo.toml",
    # etc.), so an "e2e-" prefix there permanently self-blocks its own
    # verification command. Python/pytest never hit this because a bare
    # "pytest" invocation embeds no path at all.
    scenario_id = f"harness-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_dir = root / scenario_id
    case = choose_case(root, getattr(args, "language", None))
    body = issue_body(case, scenario_id)
    issue = create_issue(args.repo, f"[E2E {scenario_id}] {case['title']}", body)
    clone_dir = Path(args.checkout_root).resolve() / scenario_id / "repo"
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    github.ensure_git_auth()
    clone = git.clone(github.clone_url(args.repo), str(clone_dir), branch="main")
    scenario = Scenario(run_dir, conductor)
    scenario.state = {
        "schemaVersion": 1,
        "scenarioId": scenario_id,
        "createdAt": utc_now(),
        "repo": args.repo,
        "case": case,
        "issue": issue,
        "checkout": clone,
        "phase": "campaign",
        "workflows": {},
        "events": [],
    }
    scenario.save()
    scenario.event("issue_created", number=issue["number"], url=issue["url"])

    registered = worker_gate(conductor, "feature_campaign")
    scenario.event("worker_gate_passed", workflow="feature_campaign", simpleTasks=registered)

    summary = f"Implements {case['title'].lower()} from issue #{issue['number']} with deterministic CLI behavior and focused tests."
    payload = {
        "repoPath": clone["repoPath"],
        "workspacePath": "",
        "instruction": body,
        "changeBranch": f"harness/{case['language']}/{scenario_id}",
        "createPr": True,
        "prBase": "main",
        "prTitle": f"{case['title']} ({scenario_id})",
        "prBody": f"## Summary\n\n{summary}",
        "prDraft": False,
        "keepWorktree": True,
        "maxBudgetUsd": args.max_budget,
        "maxTurns": args.max_turns,
        "maxParallelism": args.max_parallelism,
        "maxTasks": args.max_tasks,
        "maxWaves": args.max_waves,
        "designMaxRevisions": 3,
        "planMaxRevisions": 3,
    }
    workflow_id = conductor.start("feature_campaign", payload)
    scenario.state["campaignWorkflowId"] = workflow_id
    scenario.state["workflows"]["feature_campaign"] = [workflow_id]
    scenario.state["campaignInput"] = payload
    scenario.save()
    scenario.event("workflow_started", workflow="feature_campaign", workflowId=workflow_id)
    return scenario


def create_issue_to_pr(args: argparse.Namespace, conductor: Conductor) -> Scenario:
    """The lightweight counterpart to ``create_campaign``: no design docs, no
    planning/wave DAG, no pre-made clone -- ``issue_to_pr`` fetches the issue
    and clones the repo itself, builds its own instruction from the issue's
    title/body, and (its ``approve_gate`` SWITCH hardcodes ``mode: "auto"``,
    making the "manual"/human-approval WAIT branch unreachable) never opens a
    checkpoint a driver has to signal -- it always publishes a PR, draft only
    when the real test_cycle verification didn't pass.
    """
    root = Path(args.reports).resolve()
    scenario_id = f"harness-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_dir = root / scenario_id
    case = choose_case(root, getattr(args, "language", None))
    body = issue_body(case, scenario_id)
    issue = create_issue(args.repo, f"[E2E {scenario_id}] {case['title']}", body)
    scenario = Scenario(run_dir, conductor)
    scenario.state = {
        "schemaVersion": 1,
        "scenarioId": scenario_id,
        "createdAt": utc_now(),
        "repo": args.repo,
        "case": case,
        "issue": issue,
        "phase": "issue_to_pr",
        "workflows": {},
        "events": [],
    }
    scenario.save()
    scenario.event("issue_created", number=issue["number"], url=issue["url"])

    registered = worker_gate(conductor, "issue_to_pr")
    scenario.event("worker_gate_passed", workflow="issue_to_pr", simpleTasks=registered)

    payload = {
        "repo": args.repo,
        "issueNumber": issue["number"],
        "base": "main",
        "design": False,
        "approvePr": True,
        "maxTurns": args.max_turns,
        "maxBudgetUsd": args.max_budget,
    }
    workflow_id = conductor.start("issue_to_pr", payload)
    scenario.state["issueToPrWorkflowId"] = workflow_id
    scenario.state["workflows"]["issue_to_pr"] = [workflow_id]
    scenario.state["issueToPrInput"] = payload
    scenario.save()
    scenario.event("workflow_started", workflow="issue_to_pr", workflowId=workflow_id)
    return scenario


def drive_issue_to_pr(scenario: Scenario) -> dict[str, Any]:
    """Poll to a terminal state. No signaling: issue_to_pr's only WAIT task
    lives behind a hardcoded-unreachable SWITCH case (see create_issue_to_pr),
    so unlike drive_campaign there is no checkpoint for this driver to open."""
    workflow_id = str(scenario.state["issueToPrWorkflowId"])
    while True:
        execution = scenario.conductor.execution(workflow_id)
        scenario.snapshot(execution)
        status = str(execution.get("status") or "UNKNOWN")
        scenario.state["issueToPrStatus"] = status
        scenario.save()
        if status in TERMINAL:
            scenario.event("issue_to_pr_terminal", workflowId=workflow_id, status=status)
            return execution
        time.sleep(2)


def summarize_issue_to_pr(scenario: Scenario, execution: dict[str, Any]) -> None:
    output = execution.get("output") or {}
    scenario.state["issueToPrOutput"] = output
    scenario.state["phase"] = "issue_to_pr_complete" if execution.get("status") == "COMPLETED" else "issue_to_pr_failed"
    scenario.save()
    print(json.dumps({
        "scenarioId": scenario.state.get("scenarioId"),
        "language": (scenario.state.get("case") or {}).get("language"),
        "workflowId": scenario.state.get("issueToPrWorkflowId"),
        "status": execution.get("status"),
        "issueUrl": (scenario.state.get("issue") or {}).get("url"),
        "branch": output.get("branch"),
        "verificationCommit": output.get("verificationCommit"),
        "deliveryOutcome": output.get("deliveryOutcome"),
        "verificationState": output.get("verificationState"),
        "approvalState": output.get("approvalState"),
        "prNumber": output.get("prNumber"),
        "prUrl": output.get("prUrl"),
        "prDraft": output.get("prDraft"),
        "publicationState": output.get("publicationState"),
        "publicationReason": output.get("publicationReason"),
        "pushed": output.get("pushed"),
        "stateFile": str(scenario.state_path),
    }, indent=2, sort_keys=True))


def start_review(scenario: Scenario) -> dict[str, Any]:
    campaign = scenario.state.get("campaignOutput") or {}
    pr_number = int(campaign.get("prNumber") or 0)
    if pr_number <= 0:
        raise RuntimeError("campaign did not publish a pull request")
    registered = worker_gate(scenario.conductor, "pr_review")
    scenario.event("worker_gate_passed", workflow="pr_review", simpleTasks=registered)
    case = scenario.state.get("case") or {}
    guidance = (
        "Review the feature against issue #"
        f"{(scenario.state.get('issue') or {}).get('number')}. Give special attention to: "
        + " ".join(case.get("invariants") or [])
        + " Validate the documented unittest command and all error paths; the guidance is focus, not proof."
    )
    payload = {
        "repo": scenario.state["repo"],
        "prNumber": pr_number,
        "approve": True,
        "approvalMode": "human",
        "reviewGuidance": guidance,
        "maxInvestigationPasses": 1,
        "maxTurns": 250,
        "maxBudgetUsd": 50,
    }
    workflow_id = scenario.conductor.start("pr_review", payload)
    scenario.state.setdefault("workflows", {}).setdefault("pr_review", []).append(workflow_id)
    scenario.state["reviewWorkflowId"] = workflow_id
    scenario.state["reviewInput"] = payload
    scenario.state["phase"] = "review"
    scenario.save()
    scenario.event("workflow_started", workflow="pr_review", workflowId=workflow_id)
    execution = drive_review(scenario, workflow_id)
    scenario.state["reviewOutput"] = execution.get("output") or {}
    scenario.state["phase"] = "review_complete" if execution.get("status") == "COMPLETED" else "review_failed"
    scenario.save()
    return execution


def load_scenario(path: str, conductor: Conductor) -> Scenario:
    resolved = Path(path).resolve()
    run_dir = resolved.parent if resolved.name == "state.json" else resolved
    scenario = Scenario(run_dir, conductor)
    if not scenario.state:
        raise RuntimeError(f"no scenario state found under {run_dir}")
    return scenario


def summarize_campaign(scenario: Scenario, execution: dict[str, Any]) -> None:
    output = execution.get("output") or {}
    scenario.state["campaignOutput"] = output
    scenario.state["phase"] = "campaign_complete" if execution.get("status") == "COMPLETED" else "campaign_failed"
    scenario.save()
    print(json.dumps({
        "scenarioId": scenario.state.get("scenarioId"),
        "language": (scenario.state.get("case") or {}).get("language"),
        "workflowId": scenario.state.get("campaignWorkflowId"),
        "status": execution.get("status"),
        "issueUrl": (scenario.state.get("issue") or {}).get("url"),
        "branch": output.get("branch"),
        "commit": output.get("commit"),
        "outcome": output.get("outcome"),
        "prNumber": output.get("prNumber"),
        "prUrl": output.get("prUrl"),
        "publicationState": output.get("publicationState"),
        "stateFile": str(scenario.state_path),
    }, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "command",
        choices=("start-campaign", "resume-campaign", "start-review", "resume-review",
                 "start-issue-to-pr", "resume-issue-to-pr", "show"),
    )
    value.add_argument("scenario", nargs="?", help="scenario directory or state.json for resume/show")
    value.add_argument(
        "--language", choices=LANGUAGES, default="python",
        help="scenario language for start-campaign (default: python, preserving the original single-language behavior)",
    )
    value.add_argument("--repo", default=DEFAULT_REPO)
    value.add_argument("--server", default=os.environ.get("CONDUCTOR_SERVER_URL", DEFAULT_SERVER))
    value.add_argument("--reports", default=str(ROOT / "reports" / "real-e2e"))
    value.add_argument("--checkout-root", default="/tmp/coding-harness-real-e2e")
    value.add_argument("--max-budget", type=float, default=50)
    value.add_argument("--max-turns", type=int, default=500)
    value.add_argument("--max-parallelism", type=int, default=4)
    value.add_argument("--max-tasks", type=int, default=12)
    value.add_argument("--max-waves", type=int, default=10)
    return value


def main() -> int:
    args = parser().parse_args()
    conductor = Conductor(args.server)
    if args.command == "start-campaign":
        scenario = create_campaign(args, conductor)
        execution = drive_campaign(scenario)
        summarize_campaign(scenario, execution)
        return 0 if execution.get("status") == "COMPLETED" else 1
    if args.command == "start-issue-to-pr":
        scenario = create_issue_to_pr(args, conductor)
        execution = drive_issue_to_pr(scenario)
        summarize_issue_to_pr(scenario, execution)
        return 0 if execution.get("status") == "COMPLETED" else 1
    if not args.scenario:
        raise SystemExit(f"{args.command} requires a scenario directory or state.json")
    scenario = load_scenario(args.scenario, conductor)
    if args.command == "show":
        print(json.dumps(scenario.state, indent=2, sort_keys=True))
        return 0
    if args.command == "start-review":
        execution = start_review(scenario)
        print(json.dumps({
            "workflowId": scenario.state.get("reviewWorkflowId"),
            "status": execution.get("status"),
            "output": execution.get("output") or {},
            "drafts": scenario.state.get("reviewDrafts") or [],
            "stateFile": str(scenario.state_path),
        }, indent=2, sort_keys=True))
        return 0 if execution.get("status") == "COMPLETED" else 1
    if args.command == "resume-review":
        workflow_id = str(scenario.state.get("reviewWorkflowId") or "")
        if not workflow_id:
            raise RuntimeError("scenario has no review workflow to resume")
        execution = drive_review(scenario, workflow_id)
        scenario.state["reviewOutput"] = execution.get("output") or {}
        scenario.save()
        return 0 if execution.get("status") == "COMPLETED" else 1
    if args.command == "resume-issue-to-pr":
        if not scenario.state.get("issueToPrWorkflowId"):
            raise RuntimeError("scenario has no issue_to_pr workflow to resume")
        execution = drive_issue_to_pr(scenario)
        summarize_issue_to_pr(scenario, execution)
        return 0 if execution.get("status") == "COMPLETED" else 1
    execution = drive_campaign(scenario)
    summarize_campaign(scenario, execution)
    return 0 if execution.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
