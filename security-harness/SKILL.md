---
name: security-harness
description: Operate the Conductor security harness for authorized source analysis, web/API scanning, and deep agentic penetration testing. Use when the user wants to run or monitor sast_report, security_scan, or deep_assess; test a permitted target; triage security findings; collect reports/SARIF/dossiers; register the security workflows; or manage the harness stack. Require explicit authorization before live testing and preserve the requested capability and scope boundaries.
---

# Conductor Security Harness

Run authorized security assessments as durable Conductor workflows. Use the harness entrypoints
instead of assembling raw workflow inputs: they validate authorization, bootstrap the stack,
register definitions, start workers, and print the execution/report locations.

## Safety invariant

- Require the user to state that they own the live target or have explicit permission to test it.
- Do not infer authorization from target reachability, a company name, or credentials.
- Do not add `--authorized`, raise `--capability`, enable `--intrusive`/`--resilience`, broaden
  scope, or enable `--leave-evidence` unless the user explicitly requests that authority.
- Prefer `--manifest <file>` for real engagements. Preserve its hosts, time window, capability
  ceiling, rate/data budgets, forbidden operations, and protected records.
- Treat pasted credentials as compromised. Do not echo or reuse them; recommend rotation and ask
  for credentials through environment variables or a local session/manifest file.
- Source-only `./sast` does not contact a live target and does not require live-target authorization.

Read [`docs/authorization.md`](docs/authorization.md) before changing capability or scope. Read
[`docs/authentication.md`](docs/authentication.md) before configuring identities or SSO.

## Choose the entrypoint

| User intent | Entrypoint | Workflow |
|---|---|---|
| Analyze source without a live target | `./sast <source>` | `sast_report` |
| Run a bounded surface scan | `./scan <url>` | `security_scan` |
| Run multi-pass exploit-and-verify testing | `./assess <url>` | `deep_assess` |
| Capture a browser-authenticated session | `./sso-capture <url>` | local credential capture |

Use `./scan --source <path>` or `./assess --source <path>` when both source and a live target are
available. Use `./assess` for multi-identity, source-guided, OOB-confirmed, purple-team, or
capability-2 work. See [`docs/scan-vs-assess.md`](docs/scan-vs-assess.md) for the full comparison.

## Preflight and setup

Work from `security-harness/`.

1. Check for the `conductor` CLI, Docker, Python 3.11+, `jq`, and `curl`.
2. Require `ANTHROPIC_API_KEY` in the process environment for LLM tasks; never print it.
3. Run `make venv` once to install workers and Playwright Chromium.
4. Let `./scan`/`./assess` bootstrap a local stack, or use `--no-bootstrap` with an already managed
   server. Respect `CONDUCTOR_SERVER_URL` when it targets a remote server.
5. After workflow or task-definition changes, run `make register` and verify every SIMPLE task has
   a registered task definition.

For a safe local demonstration:

```bash
make up
./scan http://localhost:3001 --authorized
```

`make up` starts OWASP Juice Shop, an intentionally vulnerable local target. Do not substitute a
real target without the authorization preflight.

## Run and monitor

Start with the minimum capability that satisfies the request. Capability 1 is read-only at the
mutation gate; capability 2 permits synthetic state-changing tests and cleanup.

```bash
./sast /absolute/path/to/source
./scan https://target.example --manifest manifests/engagement.json
./assess https://target.example --manifest manifests/engagement.json --source /path/to/source
```

Use the workflow ID printed by the entrypoint:

```bash
conductor workflow get-execution <workflowId> -c
```

Long runs are normal. Report status, failed task and reason, capability/scope, and output paths.
Do not retry terminal policy failures until the authorization or configuration problem is fixed.

## Outputs

Read results from `reports/<scan-id>/`:

- `report.md` and `report.pdf` — human-readable report
- `findings.json` — structured findings
- `report.sarif` — SARIF 2.1.0
- `dossier.json` — deep-assessment attack graph and residual risk

For remediation verification, use `make retest DOSSIER=reports/<id>/dossier.json ...` with the
same authorization and scope. Never describe a candidate as confirmed unless the evidence and
verification status support it.

## References

- Human setup and examples: [`README.md`](README.md)
- Quickstart: [`docs/quickstart.md`](docs/quickstart.md)
- Deployment modes: [`docs/deployment.md`](docs/deployment.md)
- Architecture: [`docs/architecture.md`](docs/architecture.md)
- Output contracts: [`docs/outputs.md`](docs/outputs.md)
