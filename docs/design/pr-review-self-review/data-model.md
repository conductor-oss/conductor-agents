# pr_review self-review fallback — Data model & wiring

Reuses the exact names/signatures from `architecture.md`. This doc pins down the
`gh` calls, the rendered-markdown shape, and the workflow wiring.

## `gh` calls used

| Helper | Command | Notes |
| --- | --- | --- |
| `authenticated_login()` | `gh api user --jq .login` | Cached in a module global for the process (like `_AUTH_DONE`); `check=False`, returns `""` on any error. |
| `pr_author()` | `gh pr view {number} --repo {slug} --json author` → `.author.login` | `check=False`; `""` if missing/unparseable. |
| comment fallback | `gh pr comment {number} --body <md>` (via existing `pr_comment` path) | Appends `HARNESS_MARKER`; repo inferred from checkout or `--repo {slug}`. |
| reviews (unchanged) | `gh api repos/{slug}/pulls/{number}/reviews --method POST --input <tmp>` | The path that 422s on self-author. |

`authenticated_login()` and `pr_author()` both run from a neutral cwd via the
existing module-level `run(...)` (repo-scoped by `--repo {slug}`), so they work
before/without a local checkout — matching `pr_comments`/`pr_diff`.

## Structured review input (unchanged)

The coding_agent's `structured` output, consumed by `pr_submit_review`:

```jsonc
{
  "summary": "string (markdown)",
  "verdict": "comment" | "request_changes",
  "comments": [
    { "path": "src/x.py", "line": 42,
      "severity": "blocking|suggestion|nit|question", "body": "..." }
  ]
}
```

`verdict` still maps to `event`: `request_changes → REQUEST_CHANGES`, else
`COMMENT`. In **comment mode** the `event` is still reported in the result and
prefixed into the rendered markdown (see below), since a conversation comment
carries no formal verdict.

## `render_review_markdown(summary, comments)` output

A single markdown blob, used identically by the comment fallback body and the
local file. Shape:

```markdown
## Automated review — REQUEST_CHANGES

<summary text>

---
### Inline findings
- `src/foo.py:42` — **blocking** — <body>
- `src/bar.py:7` — <body>
```

Rules:
- Heading verdict segment is omitted / shown as `COMMENT` per `event`.
- The `### Inline findings` section is dropped entirely when `comments` is empty.
- Each finding: `` `path:line` `` prefix; `**severity**` shown only when present;
  entries with no anchorable `path`/`line` are still listed by body (nothing is
  silently dropped, unlike the reviews API which requires a valid diff anchor).

## Workflow wiring (`workflows/pr_review.json`)

The final `pr_submit_review` task gains inputs; `outputParameters` gains fields.

```jsonc
{
  "name": "pr_submit_review",
  "taskReferenceName": "submit",
  "type": "SIMPLE",
  "inputParameters": {
    "repo": "${workflow.input.repo}",
    "number": "${workflow.input.prNumber}",
    "structured": "${final_review.output.result}",
    "repoPath": "${clone.output.repoPath}",
    "writeOutputFile": "${workflow.input.writeReviewFile}",
    "outputFile": "${workflow.input.reviewOutputFile}"
  }
}
```

New workflow-level inputs (added to `inputParameters` + `inputTemplate`):

| Input | Default | Meaning |
| --- | --- | --- |
| `writeReviewFile` | `false` | Also write the review to a local file. |
| `reviewOutputFile` | `.conductor/review-output.md` | Output path (relative → under the checkout). |

New `outputParameters`:

```jsonc
"mode":       "${submit.output.mode}",
"selfReview": "${submit.output.selfReview}",
"outputFile": "${submit.output.outputFile}"
```

## Handler mapping (`gitops/tasks.py` → `pr_submit_review`)

```
repoPath        -> repo_path=i.get("repoPath")
writeOutputFile -> write_output_file=_bool(i.get("writeOutputFile"))
outputFile      -> output_file=i.get("outputFile") or ".conductor/review-output.md"
```

Log line extended: `[pr_submit_review] #<n> mode=<mode> event=<event>
inline=<n> self=<selfReview> file=<outputFile or '-'>`.
