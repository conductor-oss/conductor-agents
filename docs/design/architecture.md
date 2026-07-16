# pr_review self-author 422 fix — Architecture

## Problem

`pr_review` ends by calling `pr_submit_review`, which posts a **formal GitHub
review** through the reviews REST API (`github.submit_review` →
`POST repos/{slug}/pulls/{number}/reviews`). GitHub **rejects that request with
HTTP 422** when the authenticated `gh` user is the **same account as the PR
author** — GitHub does not allow a user to submit a formal review on their own
pull request. The existing 422 fallback in `submit_review` only strips *inline*
comments and re-`POST`s the same reviews endpoint, so a self-authored PR fails
on **both** attempts and the whole workflow errors out.

## Fix (three parts)

1. **Detect self-authorship** — compare the authenticated `gh` login against the
   PR author login *before* choosing how to post.
2. **Fall back to the comments API** — when they match (or when the reviews API
   still 422s), post the findings as ordinary PR comments (one issue comment for
   the summary/verdict + best-effort inline review comments) instead of a formal
   review submission. Comments carry no self-review restriction.
3. **Optional local file output** — write the rendered review markdown to a
   repo-local file so the review is always captured even if every network post
   is skipped or fails. The `pr_review` workflow **enables this by default** at
   the workflow layer (see "Default output path" below); the library function is
   opt-in.

## Tech stack

Unchanged from the existing harness: Python 3.11 workers, Conductor
`@worker_task` tasks, the `gh` CLI shelled out via `common.exec.run`, and
Conductor JSON workflow / task definitions. No new dependencies, no new files.

## Module / file layout

Only files touched by this change are listed; every path is relative to
`coding-harness/`.

| File | Change | Responsibility |
| --- | --- | --- |
| `workers/common/github.py` | **edit** | Add `REVIEW_OUTPUT_DEFAULT`, `viewer_login()`, `pr_author_login()`, `render_review_markdown()`, `post_review_as_comments()`; rework `submit_review()` to take `author`/`reviewer`/`output_file`/`repo_path` and route to the comments fallback + optional file write. Extend `pr_comments()` to also return `author`. |
| `workers/gitops/tasks.py` | **edit** | `pr_submit_review` task: read new inputs (`author`, `outputFile`, `repoPath`), map camelCase→snake_case, pass through to `github.submit_review`; extend the log line with `mode=`/`reason=`. |
| `workers/workflows/pr_review.json` | **edit** | Add `outputFile` to `inputParameters`/`inputTemplate` (defaulted to `REVIEW_OUTPUT_DEFAULT`); wire `author`, `outputFile`, and `repoPath` into the `submit` task; surface new outputs. |
| `workers/workflows/taskdefs/pr_submit_review.json` | **edit** | Doc string only — note the self-author fallback + `outputFile`/`repoPath` inputs. |
| `workers/tests/test_github.py` | **edit** | Unit tests for detection, comments fallback, and file output (see [testing.md](./testing.md)). |
| `workers/tests/test_gitops.py` | **edit** | `pr_submit_review` passthrough test for the new inputs + log line. |

The fallback is internal to the existing `pr_submit_review` task, so the
workflow **graph** (task list, `final_review` JSON_JQ_TRANSFORM, `approve_gate`)
is unchanged — only the `submit` task's `inputParameters` and the workflow's
`inputParameters`/`inputTemplate`/`outputParameters` gain fields.

## Control flow (`submit_review`)

```
submit_review(repo, number, summary, event, comments,
              author, reviewer, output_file, repo_path)
│
├─ reviewer = reviewer or viewer_login()          # gh api user --jq .login
├─ author   = author   or pr_author_login(slug, number)
├─ md = render_review_markdown(summary, event, comments)
│
├─ if output_file:                                # "" ⇒ skip entirely
│     path = output_file resolved against repo_path when relative
│     write md to <repo_path>/output_file, mkdir -p parent   (best-effort)
│     → fileWritten / outputPath recorded; a write error is logged, never raised
│
├─ self_author = reviewer and author and reviewer.lower() == author.lower()
│
├─ if self_author:
│     return post_review_as_comments(...)   # mode="comments", reason="self-author"
│
└─ try formal review (existing path, incl. inline-strip retry)
      └─ on 422 / any failure:
            return post_review_as_comments(...)   # mode="comments", reason="422-fallback"
```

`post_review_as_comments` posts **one issue comment** with the summary + verdict
banner (`gh pr comment` / `POST .../issues/{n}/comments`) and attempts **inline
review comments** individually (`POST .../pulls/{n}/comments`, one per finding,
each failure swallowed) so a single un-anchorable line never sinks the batch. If
inline posting is unavailable it folds the findings into the issue-comment body,
mirroring today's summary-only behavior.

## Default output path (resolves the on/off contradiction)

`REVIEW_OUTPUT_DEFAULT = ".conductor/review-output.md"` lives in
`common/github.py`. There are **two distinct defaults**, and they do not
conflict:

- **Library default (opt-in):** `submit_review(..., output_file="")` writes
  **nothing**. An empty `output_file` unconditionally skips the file-write block.
  This keeps the reusable function side-effect-free unless a caller asks for a
  file.
- **Workflow default (opt-out):** `pr_review.json`'s `inputTemplate` sets
  `outputFile` to `REVIEW_OUTPUT_DEFAULT`. So a normal `pr_review` run **always**
  writes `.conductor/review-output.md` in the checkout; a caller disables it by
  passing `outputFile: ""` in the workflow input.

This is the **single point of substitution**: the constant is referenced exactly
once in control flow (the `pr_review.json` `inputTemplate`), never inside
`submit_review`. github.py and tasks.py both treat `""` as "no file"; only the
workflow JSON turns the feature on. The smoke check (`.conductor/review-output.md`
present after a `pr_review` run) passes because the workflow default supplies the
path, and `repoPath` wiring (below) makes it land in the checkout.

## repoPath wiring (relative-path resolution)

`outputFile` defaults to the **relative** path `.conductor/review-output.md`. To
land under the PR checkout rather than the worker's cwd, the `submit` task must
receive `repoPath`. `pr_review.json` wires
`repoPath: "${clone.output.repoPath}"` into the `submit` task's
`inputParameters` (alongside `author` and `outputFile`). `submit_review` resolves
a relative `output_file` against `repo_path`; an absolute `output_file` is used
as-is; when `repo_path` is empty a relative path resolves against cwd.

## Shared contracts

These names/signatures are reused verbatim by every component and the other docs.

### `common/github.py`

```python
HARNESS_MARKER: str                      # existing — reused to tag bot comments
REVIEW_OUTPUT_DEFAULT = ".conductor/review-output.md"   # new; used only by pr_review.json

def viewer_login() -> str:
    """Authenticated gh login (`gh api user --jq .login`); "" if it fails."""

def pr_author_login(repo_or_url: str, number: int) -> str:
    """PR author login (`gh pr view --json author`); "" if unavailable."""

def render_review_markdown(summary: str, event: str,
                           comments: list[dict]) -> str:
    """Render the review to a single markdown blob: a verdict banner
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
    local file output. Returns a ReviewResult (see data-model.md)."""
```

- All comment bodies posted by the fallback are prefixed with `HARNESS_MARKER`
  so the feedback-consolidation loop in `pr_comments()` keeps skipping the
  harness's own output (re-runnable review loop preserved).
- Login comparison is case-insensitive and null-safe: if either login is empty
  the PR is treated as **not** self-authored (fail toward the normal review).

### Naming conventions

- Task input keys are camelCase (`outputFile`, `author`, `repoPath`); Python
  kwargs are snake_case (`output_file`, `author`, `repo_path`) — mapped in
  `gitops/tasks.py::pr_submit_review`.
- Fallback `reason` values are the fixed strings `"self-author"` and
  `"422-fallback"`; `""` when a formal review succeeds.
- Result dicts are plain dicts placed on task output via `common.results.ok`.

See [data-model.md](./data-model.md) for exact shapes and the markdown format,
and [testing.md](./testing.md) for the test plan.
