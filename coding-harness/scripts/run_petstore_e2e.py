#!/usr/bin/env python3
"""One-off real-e2e stress test: a full-stack pet store (Java+Spring Boot backend,
React+Node frontend) driven through feature_campaign from a real GitHub issue.

Not part of run_real_e2e.py's per-language CASES matrix -- this is a single,
deliberately complex, two-build-system scenario meant to exercise feature_campaign
at real-world scope (multiple entities, business-rule state machines, layered
backend architecture, a real frontend) and to exercise verification.py's
multi-build-system full-mode discovery (mvn AND npm in one changeset) under load.

Reuses run_real_e2e's proven Conductor/Scenario/create_issue/worker_gate/
drive_campaign/summarize_campaign machinery -- only the issue body and campaign
budget/turn/wave/task limits are custom.

    python3 scripts/run_petstore_e2e.py start
    python3 scripts/run_petstore_e2e.py resume <scenario-dir-or-state.json>
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "workers"))

import run_real_e2e as base  # noqa: E402
from common import git, github  # noqa: E402


def _pkg(scenario_id: str) -> str:
    return scenario_id.replace("-", "_")


def issue_body(scenario_id: str) -> str:
    pkg = _pkg(scenario_id)
    backend = f"apps/{pkg}/backend"
    frontend = f"apps/{pkg}/frontend"
    java_pkg = f"com.{pkg}"
    example_id = scenario_id[-8:]
    return f"""Build a self-contained, real-world pet store system: a Java 17 + Spring Boot 3 REST backend with an in-memory H2 database, and a React + TypeScript frontend that consumes it. Two independent projects under one feature directory, each its own build (no shared parent POM, no npm workspace membership with anything else in this repository). Do not modify files outside `apps/{pkg}/` and `docs/{pkg}.md`.

## Domain

Three entities with a real business-rule state machine, not a CRUD toy:

- **Category**: id, name (unique, required).
- **Pet**: id, name (required), category (required FK to Category), status (enum `AVAILABLE`, `PENDING`, `SOLD`), tags (list of strings, may be empty).
- **Order**: id, pet (required FK to Pet), quantity (positive integer), shipDate (ISO-8601 date, optional), status (enum `PLACED`, `APPROVED`, `DELIVERED`), complete (boolean, true only once DELIVERED).

## Backend -- `{backend}/` (Maven project, packaging jar)

Required exact files:
- `{backend}/pom.xml` -- Spring Boot 3.x (`spring-boot-starter-parent` or explicit BOM), dependencies limited to `spring-boot-starter-web`, `spring-boot-starter-data-jpa`, `spring-boot-starter-validation`, `com.h2database:h2` (runtime scope), `spring-boot-starter-test` (test scope). No other dependency. `spring-boot-maven-plugin` configured so `mvn test` runs the full suite without needing a separate run step.
- `{backend}/src/main/java/{java_pkg.replace('.', '/')}/PetstoreApplication.java` -- `@SpringBootApplication` main class.
- `.../model/Category.java`, `.../model/Pet.java` (with `PetStatus` enum), `.../model/Order.java` (with `OrderStatus` enum) -- JPA entities (`@Entity`), Bean Validation annotations on required fields.
- `.../repository/CategoryRepository.java`, `PetRepository.java`, `OrderRepository.java` -- Spring Data JPA interfaces; `PetRepository` supports filtering by status and category id.
- `.../service/CategoryService.java`, `PetService.java`, `OrderService.java` -- all business rules below live here, not in controllers.
- `.../web/CategoryController.java`, `PetController.java`, `OrderController.java` -- REST controllers under `/api/categories`, `/api/pets`, `/api/orders`; request/response DTOs in `.../web/dto/` (entities never appear directly on the wire).
- `.../web/GlobalExceptionHandler.java` -- `@ControllerAdvice` mapping validation failures to `400` with a field-level error body, and business-rule violations (see below) to `409` with a stable `{{"error": "<code>", "message": "..."}}` body.
- `{backend}/src/main/resources/application.properties` -- H2 in-memory datasource (`jdbc:h2:mem:...`), `spring.jpa.hibernate.ddl-auto=create-drop` (or equivalent), server started on a random/default port (tests must not hardcode a port).
- `{backend}/src/test/java/.../PetServiceTest.java`, `OrderServiceTest.java` -- unit tests (Mockito or a real in-memory repository) covering every business rule below, including its rejected/invalid cases.
- `{backend}/src/test/java/.../PetControllerIntegrationTest.java`, `OrderControllerIntegrationTest.java` -- `@SpringBootTest` + `MockMvc` (or `@WebMvcTest` + mocked service, your choice, but at least one test class must exercise the real Spring context end-to-end against the real H2 database), covering the full request/response cycle including `400`/`409` error bodies.

### Business rules (behavioral invariants -- every one needs a passing AND a rejected-case test)

- Pet status only moves forward `AVAILABLE -> PENDING -> SOLD`; `SOLD` is terminal. A `PENDING` pet may revert to `AVAILABLE` (order cancelled), but a `SOLD` pet can never change status again. Any other transition is a `409` with error code `invalid_pet_transition`.
- Creating an order for a pet that is not `AVAILABLE` is rejected with `409` (`pet_not_available`); on success the pet moves to `PENDING` and the order is created as `PLACED`, atomically.
- Order status only moves forward `PLACED -> APPROVED -> DELIVERED`; approving/delivering out of order is `409` (`invalid_order_transition`). Delivering an order sets `complete=true` and, in the same transaction, moves its pet to `SOLD`.
- Cancelling an order is only allowed while `PLACED` or `APPROVED` (never once `DELIVERED`); cancelling reverts the pet to `AVAILABLE` and marks the order accordingly (your choice of a `CANCELLED` status or a boolean -- document whichever you pick).
- Deleting a category that still has at least one pet referencing it is rejected with `409` (`category_in_use`) -- a service-layer guard, not just a database foreign-key error.
- Listing pets (`GET /api/pets`) supports optional `status` and `categoryId` query filters, applied server-side.
- Every create/update endpoint validates required fields via Bean Validation and returns `400` with a field-level error body on invalid input (e.g. blank name, non-positive quantity).

Run the backend suite with:

`mvn -f {backend}/pom.xml test`

## Frontend -- `{frontend}/` (Vite + React + TypeScript project)

A real interactive UI, not a static page: it calls the backend's REST contract through a typed API client, but its own automated tests must be fully hermetic -- mock the API client module (or `fetch`), never require a live backend server, exactly the pattern already proven in this repository's own React scenarios.

Required exact files:
- `{frontend}/package.json` (`"type": "module"`; `dependencies` limited to `react`, `react-dom`; `devDependencies` limited to `vite`, `@vitejs/plugin-react`, `typescript`, `vitest`, `@testing-library/react`, `@testing-library/jest-dom`, `jsdom`; scripts `"build": "vite build"` and `"test": "vitest run"`).
- `{frontend}/vite.config.ts` (React plugin; `test` block with `environment: "jsdom"` and a setup file).
- `{frontend}/tsconfig.json`, `{frontend}/index.html`, `{frontend}/src/main.tsx`.
- `{frontend}/src/types.ts` -- TypeScript types mirroring the backend DTOs (Category, Pet, PetStatus, Order, OrderStatus).
- `{frontend}/src/api/client.ts` -- a thin, pure `fetch`-based client (`listCategories`, `listPets(filters)`, `createPet`, `updatePetStatus`, `listOrders`, `createOrder`, `advanceOrderStatus`, `cancelOrder`, `deleteCategory`) -- no other module may call `fetch` directly.
- `{frontend}/src/App.tsx` -- a tabbed or sectioned view covering Categories, Pets (with status/category filters and a way to change a pet's status), and Orders (create an order for a pet, advance/cancel an order). Imports only `./api/client`, `./types`, and its own component files -- no state-management or routing library.
- At least three focused component files under `{frontend}/src/components/` (e.g. `PetList.tsx`, `PetForm.tsx`, `OrderList.tsx` or similar -- your choice of decomposition, but `App.tsx` itself must stay a thin composition layer).
- `{frontend}/src/setupTests.ts` -- must read exactly:
  ```ts
  import {{ afterEach, expect }} from 'vitest';
  import {{ cleanup }} from '@testing-library/react';
  import * as matchers from '@testing-library/jest-dom/matchers';

  expect.extend(matchers);
  afterEach(cleanup);
  ```
  (Not the bare `import '@testing-library/jest-dom';` side-effect form: Vitest does not inject a Jest-style global `expect` by default, so that form fails every test with `ReferenceError: expect is not defined`; and without an explicit `afterEach(cleanup)`, nothing unmounts between tests, so a second test's `render()` leaves the first test's DOM in place and a query like `getByLabelText` throws "multiple elements found" the moment a second test runs -- both confirmed live in this repository.)
- `{frontend}/src/api/client.test.ts` -- unit tests mocking global `fetch`, covering at least one success and one error-response case per client function's category (categories/pets/orders).
- `{frontend}/src/App.test.tsx` (and/or per-component test files) -- component tests via React Testing Library, mocking `./api/client` (never real `fetch`), covering: listing pets with a status filter applied, creating a pet, placing an order and seeing the pet's status reflect `PENDING`, and rendering a `409`-style error message returned by a mocked client call. Include the unique example identifier `{example_id}` in at least one fixture (e.g. a pet or category name) and assert on it somewhere in the rendered output.

Run the frontend suite with:

`npm --prefix {frontend} test`

## Documentation

`docs/{pkg}.md` -- architecture overview (backend layers, frontend structure), the full REST API surface as a table (method, path, request/response shape), the pet and order state-transition tables, every business rule above, and both build/test commands.

## Constraints

- Backend: only the Maven dependencies listed above, nothing else, in any scope.
- Frontend: only the npm dependencies listed above, nothing else, in either `dependencies` or `devDependencies`.
- No Docker, no docker-compose, no external database -- H2 in-memory only.
- Keep each class/module independently understandable; this is a real system, not a single file, so respect the layer boundaries above (controllers never touch repositories directly, the frontend API client is the only thing that calls `fetch`).
"""


def create_petstore_campaign(args: argparse.Namespace, conductor: "base.Conductor") -> "base.Scenario":
    root = Path(args.reports).resolve()
    # Not "e2e"/"harness" + a second "petstore" baked in twice: keep the resulting
    # Java package (dashes -> underscores, dotted onto "com.petstore.") and the
    # apps/ directory name short and legible. "petstore" itself doesn't collide
    # with verification.py's _HEAVY_MARKERS (docker/e2e/integration/etc.).
    scenario_id = f"petstore-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_dir = root / scenario_id
    title = "Build a full-stack pet store (Spring Boot + React, in-memory H2)"
    body = issue_body(scenario_id)
    issue = base.create_issue(args.repo, f"[E2E {scenario_id}] {title}", body)
    clone_dir = Path(args.checkout_root).resolve() / scenario_id / "repo"
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    github.ensure_git_auth()
    clone = git.clone(github.clone_url(args.repo), str(clone_dir), branch="main")
    scenario = base.Scenario(run_dir, conductor)
    scenario.state = {
        "schemaVersion": 1,
        "scenarioId": scenario_id,
        "createdAt": base.utc_now(),
        "repo": args.repo,
        "case": {"slug": "petstore-fullstack", "title": title, "language": "java+spring-boot / react+node"},
        "issue": issue,
        "checkout": clone,
        "phase": "campaign",
        "workflows": {},
        "events": [],
    }
    scenario.save()
    scenario.event("issue_created", number=issue["number"], url=issue["url"])

    registered = base.worker_gate(conductor, "feature_campaign")
    scenario.event("worker_gate_passed", workflow="feature_campaign", simpleTasks=registered)

    summary = f"Implements {title.lower()} from issue #{issue['number']}: a Spring Boot + H2 backend and a React + TypeScript frontend, both with real automated tests."
    payload = {
        "repoPath": clone["repoPath"],
        "workspacePath": "",
        "instruction": body,
        "changeBranch": f"harness/petstore/{scenario_id}",
        "createPr": True,
        "prBase": "main",
        "prTitle": f"{title} ({scenario_id})",
        "prBody": f"## Summary\n\n{summary}",
        "prDraft": False,
        "keepWorktree": True,
        "maxBudgetUsd": args.max_budget,
        "maxTurns": args.max_turns,
        "maxParallelism": args.max_parallelism,
        "maxTasks": args.max_tasks,
        "maxWaves": args.max_waves,
        "designMaxRevisions": args.design_max_revisions,
        "planMaxRevisions": args.plan_max_revisions,
    }
    workflow_id = conductor.start("feature_campaign", payload)
    scenario.state["campaignWorkflowId"] = workflow_id
    scenario.state["workflows"]["feature_campaign"] = [workflow_id]
    scenario.state["campaignInput"] = payload
    scenario.save()
    scenario.event("workflow_started", workflow="feature_campaign", workflowId=workflow_id)
    return scenario


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("command", choices=("start", "resume", "show"))
    value.add_argument("scenario", nargs="?")
    value.add_argument("--repo", default=base.DEFAULT_REPO)
    value.add_argument("--server", default=base.os.environ.get("CONDUCTOR_SERVER_URL", base.DEFAULT_SERVER))
    value.add_argument("--reports", default=str(base.ROOT / "reports" / "real-e2e"))
    value.add_argument("--checkout-root", default="/tmp/coding-harness-real-e2e")
    # Scaled up from run_real_e2e.py's single-file-scenario defaults: this is a
    # multi-entity, layered, two-build-system real application, easily 3x the
    # subtask count of a reconciler scenario.
    value.add_argument("--max-budget", type=float, default=150)
    value.add_argument("--max-turns", type=int, default=800)
    value.add_argument("--max-parallelism", type=int, default=4)
    value.add_argument("--max-tasks", type=int, default=30)
    value.add_argument("--max-waves", type=int, default=15)
    value.add_argument("--design-max-revisions", type=int, default=4)
    value.add_argument("--plan-max-revisions", type=int, default=4)
    return value


def main() -> int:
    args = parser().parse_args()
    conductor = base.Conductor(args.server)
    if args.command == "start":
        scenario = create_petstore_campaign(args, conductor)
        execution = base.drive_campaign(scenario)
        base.summarize_campaign(scenario, execution)
        return 0 if execution.get("status") == "COMPLETED" else 1
    if not args.scenario:
        raise SystemExit(f"{args.command} requires a scenario directory or state.json")
    scenario = base.load_scenario(args.scenario, conductor)
    if args.command == "show":
        print(base.json.dumps(scenario.state, indent=2, sort_keys=True))
        return 0
    execution = base.drive_campaign(scenario)
    base.summarize_campaign(scenario, execution)
    return 0 if execution.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
