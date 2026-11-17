# Permissions: what the agents need and are not allowed

Status: built 2026-11-14.

## The rule

When an agent is refused something it needs and the swarm cannot get there within what it is allowed,
it does not keep trying. It asks, and the run waits for the answer. The approver is a person or another
agent, deciding through one API.

Run c5fbbe5b is why this exists. Codex's sandbox kept `.codex/INSTALL.md` read-only. For six rounds the
implementers and the architect agreed on the one-line fix, the sandbox refused every write, and each round
re-issued the same work.

## How a request is decided

1. **The agent reports it.**
   - An implementer that is refused something (a denied write, an unreachable host, a decision outside its
     authority) lists it under `permissions` in its report (`kind`: `write`, `network` or `decision`;
     `target`; `why`) and finishes what it can.
   - The round review (the architect) judges which requests the work really needs and returns those under
     `permissions`. The architect is the first tie-breaker.
2. **`swarm_permissions`** runs after the round's decision and settles each request, in order, by:
   1. **The run's own history:** a request decided earlier on the run is never asked again.
   2. **The repository's standing grants:** an approver chose "always allow in this repository" on an
      earlier run. These are kept under `~/.conductor-harness/permissions/`.
   3. **The policy:**
      - **Allowed:** the repository's own files.
      - **Never:**
        - anything outside the repository;
        - `.git`, which the harness writes when it commits for the agents.
      - **Left to an approver:**
        - the agents' and sandboxes' own configuration (`.codex/config.toml`, `.claude/settings*.json`,
          `.conductor-code/permissions.json`);
        - any network host the harness does not already reach;
        - every decision.
   4. **An approver:** the request stays pending and the run waits, with a heartbeat, until it is
      decided. `permissionWaitMinutes` bounds the wait; `0`, the default, waits until answered.
3. **The next round gets the answers:**
   - granted paths become writable roots in the agents' sandbox (`writableRoots`);
   - granted hosts are added to the reachable hosts (`allowedDomains`);
   - every decision, with its note, is in the implementers' and the architect's prompts.

## The API (the console, port 8761)

| Call | What it does |
|---|---|
| `GET /api/permissions?run=<id>` | That run's pending requests and its decisions |
| `GET /api/permissions` | Every running swarm's pending requests, for an approver that watches all runs |
| `POST /api/permissions/decide` `{"run", "id", "decision": "grant" \| "deny", "scope": "run" \| "repository", "note"}` | Decide one request |

The POST is signed like every console write (`Authorization: Bearer <actor token>`). The decision is
recorded as that actor: `human:<email>` for a person, `agent:<name>` for an agent. An agent approver is
an ordinary client of this API, holding its own actor token.

The run page shows a **Waiting for approval** panel for pending requests, with three choices: **Allow for
this run**, **Always allow in this repository** and **Deny**. Each takes an optional note for the team.

## Where it lives

- `workers/common/permissions.py`: requests, decisions, standing grants, the policy.
- `workers/swarm/approval.py`: `swarm_permissions`, the step that waits.
- The `swarm` workflow's round loop: `… decide → permissions → next`.
- `workers/common/codex.py`: `writable_roots` opens granted paths in the Codex sandbox.
- `tui/console.py` and `tui/console.html`: the API and the panel.
