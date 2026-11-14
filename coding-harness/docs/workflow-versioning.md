# Workflow versioning policy

This harness does not use workflow versioning. There is exactly one version of each workflow —
**version 1** — on disk and on the server. See AGENTS.md's Conductor Workflow Version Policy for
the rule and why it is enforced rather than advisory.

- Every workflow definition must declare `"version": 1`.
- Update version 1 in place; never create a new workflow version.
- Never pass a version when starting a workflow — not on `POST /workflow/{name}`, a schedule's
  `startWorkflowRequest`, a `START_WORKFLOW` task, or a `subWorkflowParam`. An unversioned start
  runs the *highest* registered version, which is why stray versions are deleted (below) rather
  than worked around by pinning; a pinned start 404s when the registered version differs.

## Deploying a change

`workers/register.sh` is the canonical deployment path — run it with `make register`. It registers
via direct REST, never the `conductor` CLI: the CLI re-serializes definitions from its own internal
model and silently drops fields it does not know, including `inputSchema`/`outputSchema`.

The script does not validate task types, versions, or task definitions before registering:

1. Upserts every task definition and workflow, sub-workflows first; a workflow file not in
   `WORKFLOW_ORDER` is registered after the listed ones.
2. Deletes every registered version other than the local one, keeping (and reporting) a version
   that still has a `RUNNING` or `PAUSED` execution.

Removing a stray version is no longer a manual post-deployment step.

## Checking compliance

```bash
python3 scripts/audit_harness_invariants.py
```

Reports `REGISTERED_VERSION` for any workflow whose server versions differ from the local
definition. With no reachable server it reports the check as skipped, never as passed.

To inspect one workflow directly:

```bash
curl -s "$CONDUCTOR_SERVER_URL/metadata/workflow/<name>/versions" | jq '[.[].version]'   # → [1]
```
