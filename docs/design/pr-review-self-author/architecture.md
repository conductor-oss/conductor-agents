# pr_review self-author fallback — Architecture

## Problem

`pr_review` ends by calling `pr_submit_review`, which posts a **formal GitHub review**
through the reviews REST API (`github.submit_review` →
`POST repos/{slug}/pulls/{number}/reviews`). GitHub **rejects that request with HTTP
422** when the authenticated `gh` user is the **same account as the PR author** —
GitHub does not allow a user to submit a formal review on their own pull request. The
existing 422 fallback in `submit_review` only strips *inline* comments and re-`POST`s
the same reviews endpoint, so a self-authored PR fails on **both** attempts and the
whole workflow errors out.

## Fix (three parts)

1. **Detect self-authorship** — compare the authenticated `gh` login against the PR
   author login *before* choosing how to post.
2. **Fall back to the comments API** — when they match (or when the reviews API still
   422s), post the findings as ordinary PR comments (one issue comment for the summary
   + inline review comments for anchored findings) instead of a formal review
   submission. Comments carry no self-review restriction.
3. **Optional local file output** — write the rendered review markdown to a repo-local
   file (default `.conductor/review-output.md`) so the review is always captured even
   when every network post is skipped or fails.

## Tech stack

Unchanged from the existing harness: Python 3.11 workers, Conductor `@worker_task`
tasks, the `gh` CLI shelled out via `common.exec.run`, and Conductor JSON workflow /
task definitions. No new dependencies.

## Module / file layout

Only touched files are listed; every path is relative to `coding-harness/`.

| File | Change | Responsibility |
| --- | --- | --- |
| `workers/common/github.py` | **edit** | Add `viewer_login()`, `pr_author_login()`, `render_review_markdown()`, `post_review_as_comments()`; rework `submit_review()` to take `author`/`reviewer`/`output_file`/`repo_path` and route to the comments fallback + optional file write. Extend `pr_comments()` to also return `author`. Define `REVIEW_OUTPUT_DEFAULT`. |
| `workers/gitops/tasks.py` | **edit** | `pr_submit_review` task: read new inputs (`author`, `outputFile`, `repoPath`), map camelCase→snake_case, pass through to `github.submit_review`; extend the log line with `mode=`/`reason=`. |
| `workers/workflows/pr_review.json` | **edit** | Add `outputFile` to `inputParameters`; set `outputFile` default in `inputTemplate`; wire `author`, `outputFile`, and `repoPath` into the `submit` task; surface new outputs. |
| `workers/workflows/taskdefs/pr_submit_review.json` | **edit** | Doc string only — note the self-author fallback + `outputFile`/`repoPath` inputs. |
| `workers/tests/test_github.py` | **edit** | Unit tests for detection, comments fallback, file output (see testing.md). |

No new files, no new task definitions, no new workflows — the fallback is internal to
the existing `pr_submit_review` task, so the workflow graph is unchanged.

## Control flow (`submit_review`)

```
submit_review(repo, number, summary, event, comments,
              author, reviewer, output_file, repo_path)
│
├─ reviewer = reviewer or viewer_login()          # gh api user --jq .login
├─ author   = author   or pr_author_login(slug, number)
├─ md = render_review_markdown(summary, event, comments)
│
├─ if output_file:                                 # "" ⇒ SKIP write (see below)
│     path = <repo_path>/output_file if repo_path else output_file
│     write md to path  (mkdir -p parent; best-effort, never raises)
│     → fileWritten / outputPath
│
├─ self_author = reviewer and author and reviewer.lower() == author.lower()
│
├─ if self_author:
│     return post_review_as_comments(...)          # mode="comments", reason="self-author"
│
└─ try formal review (existing path, incl. inline-strip retry)
      └─ on 422 / any failure:
            return post_review_as_comments(...)     # mode="comments", reason="422-fallback"
```

`post_review_as_comments` posts **one issue comment** with the summary + verdict banner
(`gh pr comment` / `POST .../issues/{n}/comments`) and attempts **inline review
comments** individually (`POST .../pulls/{n}/comments`, one per finding, each failure
swallowed) so a single un-anchorable line never sinks the batch. If inline posting is
unavailable it folds the findings into the issue-comment body, mirroring today's
summary-only behavior.

## Local-file-output default — where it is substituted

There is exactly **one** default substitution, and it lives in the **workflow**, not the
library. This resolves the earlier contradiction ("when enabled" vs. default `""`):

- **Library contract (safe default = OFF).** `github.submit_review(..., output_file="")`
  writes **nothing** — an empty `output_file` is the sole disable switch. The write
  branch is skipped entirely and the result carries `fileWritten=False`, `outputPath=""`.
  Verified by a dedicated test (testing.md §"empty output_file writes nothing").
- **Workflow contract (effective default = ON).** `pr_review.json` `inputTemplate` sets
  `"outputFile"` to the literal `".conductor/review-output.md"`, which **must equal
  `REVIEW_OUTPUT_DEFAULT`**. The constant is the canonical value; the JSON copies it
  because Conductor JSON cannot reference a Python symbol. So a normal `pr_review` run
  **always** writes `.conductor/review-output.md` into the checkout (satisfies the smoke
  check).
- **Opt out** by passing `outputFile: ""` as a workflow input; it overrides the template
  default and reaches the library's no-write path.

`REVIEW_OUTPUT_DEFAULT` is therefore **not** dangling: it is referenced as the canonical
value copied into `pr_review.json`'s `inputTemplate`, and `output_file == ""` is the
unambiguous disable.

## repoPath wiring

`repo_path` is a first-class input of both the task and the library so a relative
`output_file` resolves against the **checkout**, not the worker cwd:

- `pr_review.json` passes `"repoPath": "${clone.output.repoPath}"` into the `submit`
  task's `inputParameters` (alongside `author` and `outputFile`) — see
  [data-model.md](./data-model.md).
- `gitops/tasks.py::pr_submit_review` maps `repoPath` → `repo_path` and forwards it.
- `submit_review` joins `output_file` onto `repo_path` when both are set. Without this,
  `.conductor/review-output.md` would resolve against the worker process's cwd and the
  smoke check ("written in the checkout") would fail.

## Untouched pieces (confirmed)

- The `final_review` `JSON_JQ_TRANSFORM` is **unchanged**: its `queryExpression` still
  chooses `.gated` vs `.auto`, and `final_review.output.result` still feeds the `submit`
  task's `structured` input. No new task consumes or rewrites it.
- The `submit` log line only **appends** `mode=`/`reason=` to the existing
  `ok(task, out, [...])` call (`gitops/tasks.py:281`); the pre-existing
  `event=`/`inline=` fields are preserved (see [data-model.md](./data-model.md)).

## Shared contracts

These names/signatures are reused verbatim by every component and by the other docs.

### `common/github.py`

```python
HARNESS_MARKER: str                      # existing — reused to tag bot comments
REVIEW_OUTPUT_DEFAULT = ".conductor/review-output.md"   # canonical default value

def viewer_login() -> str:
    """Authenticated gh login (`gh api user --jq .login`); "" if it fails."""

def pr_author_login(repo_or_url: str, number: int) -> str:
    """PR author login (`gh pr view --json author`); "" if unavailable."""

def render_review_markdown(summary: str, event: str,
                           comments: list[dict]) -> str:
    """Render the review to one markdown blob: a verdict banner
    (COMMENT / REQUEST_CHANGES), the summary, then a bulleted findings list
    (`- `path:line` [severity] — body`). Used for both the comments-fallback
    body and the local output file, so on-GitHub and on-disk text match."""

def post_review_as_comments(repo_or_url: str, number: int, *, summary: str,
                            event: str, comments: list[dict],
                            reason: str) -> dict:
    """Post findings via the comments API (issue comment + best-effort inline
    review comments). Returns a ReviewResult with mode="comments"."""

def submit_review(repo_or_url: str, number: int, *, summary: str,
                  event: str = "COMMENT", comments: list | None = None,
                  author: str = "", reviewer: str = "",
                  output_file: str = "", repo_path: str = "") -> dict:
    """Formal review with self-author + 422 fallback to comments, plus optional
    local file output (output_file="" ⇒ no file). Returns a ReviewResult."""
```

- All comment bodies posted by the fallback are prefixed with `HARNESS_MARKER` so the
  feedback-consolidation loop in `pr_comments()` keeps skipping the harness's own output
  (re-runnable review loop preserved).
- `output_file` is resolved relative to `repo_path` when both are given, else treated
  as-is; parent dirs are created; write failures are logged and swallowed
  (`fileWritten=False`), never raised. `output_file == ""` skips the write branch.
- Login comparison is case-insensitive and null-safe: if either login is empty the PR is
  treated as **not** self-authored (fail toward attempting the normal review).

### ReviewResult (return shape of `submit_review` / `post_review_as_comments`)

See [data-model.md](./data-model.md) for the exact keys.

### Naming conventions

- Task input keys are camelCase (`outputFile`, `author`, `repoPath`); Python kwargs are
  snake_case (`output_file`, `author`, `repo_path`) — mapped in
  `gitops/tasks.py::pr_submit_review`.
- Default output path constant: `REVIEW_OUTPUT_DEFAULT = ".conductor/review-output.md"`
  in `common/github.py`; `pr_review.json`'s `inputTemplate` copies this literal value.
- Fallback `reason` values are the fixed strings `""`, `"self-author"`, `"422-fallback"`.

See [data-model.md](./data-model.md) for exact shapes and the markdown format, and
[testing.md](./testing.md) for the test plan.
