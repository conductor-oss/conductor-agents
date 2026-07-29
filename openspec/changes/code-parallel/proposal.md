## Why

The `issue_to_pr` harness resolves GitHub issues autonomously, so the fidelity of the issue text handed to the OpenSpec planner directly determines whether the generated plan matches what was actually requested. We suspect the issue description may be silently cut off, escaped, or otherwise mangled somewhere between the GitHub API and `openspec new change`, which would cause the planner to work from an incomplete prompt without any visible error — a class of failure that is hard to notice after the fact.

## What Changes

- Produce a written investigation **report** (markdown) that traces the full path of a GitHub issue's title/body from fetch to planner, with concrete `file:line` references at each hop.
- Document the actual data flow found:
  - `issue_fetch` fetches `body` verbatim via `gh issue view --json ...,body` (`common/github.py:89-104`); the only truncation at this stage is cosmetic — `title[:80]` in the log summary (`gitops/tasks.py:224`) — and does not affect the forwarded data.
  - `issue_to_pr.json:60` inlines `${issue.output.title}` and `${issue.output.body}` into the `instruction` string via Conductor template substitution.
  - `instruction` flows unchanged through `code_parallel` → `openspec_plan.json` and reaches the planner via **two** paths: (a) as `openspec_new_change`'s `description` input (`openspec_plan.json:25`), and (b) as `goal` into the artifact-drain loop (`openspec_plan.json:41`).
  - Path (a): `openspecops/tasks.py:23` → `openspec_cli.new_change` passes it as a single `--description <body>` CLI argument (`common/openspec_cli.py:43-47`).
  - Path (b): `goal` is inlined into the artifact-writer agent's prompt (`openspec_generate_artifact.json:28`), which is the text that actually drives content generation for each artifact.
- Establish the key finding: path (b) is the **authoritative** full-fidelity channel — the planning agent receives the complete issue body verbatim via `goal`, independent of `--description`. Any truncation the external `openspec new change` CLI applies to `--description` is therefore *bypassed* for actual content generation, so `--description` is not the bottleneck it is commonly assumed to be. Note also that the `--description` argv is passed through `common/exec.run`'s `subprocess.run` with an argv list and no `shell=True` (`common/exec.py:28-31`), so there is no shell-quoting stage that could mangle or truncate the body.
- Identify and assess the remaining candidate loss points that are **not** hard-capped today but could still corrupt or drop content: the `openspec new change --description` CLI itself possibly treating the description as a short summary/first line when seeding `proposal.md` (a *silent* truncation on path (a), downstream of this repo and not visible here, but bypassed by path (b)), Conductor re-interpolation of `${...}`/dollar-brace sequences appearing inside the issue body, Conductor task input/output payload-size limits on large bodies, JSON-string escaping of quotes/backslashes/newlines during JQ/template substitution (JQ preserves JSON string escaping, so expected safe but worth spot-checking), and OS `ARG_MAX`/per-arg `MAX_ARG_STRLEN` limits when a very large body is passed as one command-line argument to `openspec new change` (which fail hard with E2BIG rather than truncate).
- State clear findings and recommendations for follow-up (verification tests, guardrails, or a hardened ingestion path) so a later change can act on them.
- No code changes: this change delivers analysis and a specification of the required ingestion behavior only.

## Capabilities

### New Capabilities
- `harness-issue-ingestion`: Defines the required behavior for pulling a GitHub issue and forwarding its title/body to the OpenSpec planner in the `issue_to_pr`/`code_parallel` pipeline — that the full description reaches `openspec new change` without truncation, escaping loss, or template-interpolation corruption — and captures the traced data flow and known risk points as the contract for any future hardening.

### Modified Capabilities
<!-- The issue-fetch → instruction-assembly path is not covered by any existing spec
     (harness-openspec-planning specs the planner loop, starting from an already-assembled
     instruction). No existing requirements change. -->

## Impact

- **Code investigated (read-only, no changes):** `coding-harness/workers/common/github.py` (`issue_fetch`), `coding-harness/workers/gitops/tasks.py` (`issue_fetch` task), `coding-harness/workers/workflows/issue_to_pr.json` (instruction assembly), `coding-harness/workers/workflows/openspec_plan.json` (`description` and `goal` wiring), `coding-harness/workers/openspecops/tasks.py` (`openspec_new_change`), `coding-harness/workers/common/openspec_cli.py` (`new_change --description`), `coding-harness/workers/workflows/openspec_generate_artifact.json` (`goal` embedded in the agent prompt).
- **Systems:** Conductor template/variable substitution, the `gh` CLI (GitHub API), and the `openspec` CLI argument handling.
- **Deliverable:** a new investigation report file plus this OpenSpec change's spec; no runtime, API, or dependency changes.
