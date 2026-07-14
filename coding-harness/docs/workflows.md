# Workflows

## Choose a workflow

| Intent | Workflow | Required inputs |
|---|---|---|
| Turn a GitHub issue into a pull request | `issue_to_pr` | `repo`, `issueNumber` |
| Review a pull request and post findings | `pr_review` | `repo`, `prNumber` |
| Address existing PR feedback | `address_pr` | `repo`, `prNumber` |
| Implement a multi-part local change | `code_parallel` | `repoPath`, `instruction` |
| Smoke-test GitHub connectivity | `github_demo` | `repoUrl`, `instruction` |

`design_docs` and `code_subtask` are internal sub-workflows. Let `code_parallel` invoke them.

## Design gate

For `code_parallel`, `issue_to_pr`, and the parallel `address_pr` engine, explicitly choose
`design:true` or `design:false`.

With design enabled, `design_docs` runs a bounded author/review loop. Human review is the default:
Approve continues to coding; Request changes submits editable feedback for another design pass.
Set `designHumanApproval:false` to use the structured, read-only `coding_agent` judge instead.
It reads the design documents with only `Read`, `Grep`, and `Glob`, then returns schema-validated
`approved` and `feedback` fields without modifying the repository.

## Backends and limits

Use `claude` (default), `codex`, or `gemini`. `code_parallel` can use different planning and coding
backends through `planAgent` and `codeAgent`. Shipped workflow defaults use at least 250 turns and a
`$50` maximum budget for every applicable agent task. Planning, parallel coding, and design-author
sessions in `code_parallel` default to 500 turns. Override these caps only intentionally.

Turn and spend caps are agent limits, not wall-clock deadlines. Runtime timeouts belong only to
the referenced Conductor task definition. There is no workflow `timeoutS` input and no secondary
worker/backend deadline.

## Publication gates

The TUI defaults to review gates before posting a PR review or opening an issue-resolution PR.
Raw CLI/API runs default those publication gates off unless `approve:true` (`pr_review`) or
`approvePr:true` (`issue_to_pr`) is supplied.

## Registration

After changing definitions:

```bash
./run.sh register
```

Registration updates sub-workflows first and verifies every SIMPLE task has a registered task
definition. A workflow that reaches an unregistered SIMPLE task will wait indefinitely.
