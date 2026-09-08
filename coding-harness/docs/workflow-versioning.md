# Workflow versioning policy

This harness does not use workflow versioning. There is exactly one version of each workflow —
**version 1** — on disk and on the server. See AGENTS.md's Conductor Workflow Version Policy for
the rule and why it is enforced rather than advisory.

- Every workflow definition must declare `"version": 1`.
- Every `SUB_WORKFLOW` reference must pin its child to version `1`.
- Update version 1 in place; never create a new workflow version.
- Never start a workflow unversioned: an unversioned `POST /workflow/{name}` runs the *highest*
  registered version, so any stray version silently shadows the definition on disk.

## Deploying a change

`workers/register.sh` is the canonical deployment path — run it with `make register`. It registers
via direct REST, never the `conductor` CLI: the CLI re-serializes definitions from its own internal
model and silently drops fields it does not know, including `inputSchema`/`outputSchema`.

The script enforces this policy end to end, failing loudly rather than deploying something that
violates it:

1. Refuses any local workflow that is not version 1, any `SUB_WORKFLOW` reference (static or
   dynamically generated) that does not pin the current version, and any workflow file missing from
   `WORKFLOW_ORDER` — an unlisted file would be neither registered nor pruned.
2. Upserts every task definition and workflow, sub-workflows first.
3. Deletes every registered version other than 1, refusing if one still has a `RUNNING` or
   `PAUSED` execution. Let those finish or terminate them, then rerun.

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
