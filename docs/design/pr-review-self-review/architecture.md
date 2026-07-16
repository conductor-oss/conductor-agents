# pr_review self-review fallback — Architecture

## Problem

`pr_review` ends by POSTing to the GitHub **reviews** API
(`repos/{slug}/pulls/{number}/reviews`). GitHub returns **HTTP 422
("Unprocessable Entity")** when the authenticated `gh` user is the **same
account as the PR author** — GitHub forbids submitting a formal review on your
own pull request. This is common in the harness because the bot posts under the
same account that opened the PR (see the `HARNESS_MARKER` note in
`common/github.py`). The whole `pr_submit_review` task then FAILs and the review
findings are lost.

The existing 422 retry inside `submit_review` only strips *inline* comments and
re-POSTs to the **same** reviews endpoint — a self-author 422 is not about a bad
line anchor, so that retry fails too.

## Fix (three parts)

1. **Detect self-review** — compare the authenticated `gh` login against the PR
   author login *before* choosing an API. When equal, skip the reviews API.
2. **Comment fallback** — post the review as an ordinary PR **conversation
   comment** via the issue-comments API (`gh pr comment`, which the harness
   already uses in `pr_comment`), folding inline findings into the comment body.
   This never 422s for a self-author. Also used as the catch-all if the reviews
   API 422s for any *other* reason.
3. **Local file output** — optionally also write the rendered review markdown to
   a file under the checkout (default `.conductor/review-output.md`) so the
   result is captured on disk regardless of which post path ran.

## Tech stack

Unchanged: Python 3, the `gh` CLI shelled through `common/exec.run`, Conductor
`@worker_task` handlers, and JSON workflow/taskdef definitions. No new
dependencies. All new logic is additive to existing modules.

## Module / file layout

Every file below already exists; this change edits them in place — **no new
source files**.

| File | Responsibility (change) |
| --- | --- |
| `coding-harness/workers/common/github.py` | Add `authenticated_login()`, `pr_author()`, `render_review_markdown()`, `write_review_file()`; add self-review detection + comment fallback + optional file output to `submit_review()`. Reuses `HARNESS_MARKER`, `repo_slug`, `_gh`, `run`. |
| `coding-harness/workers/gitops/tasks.py` | Extend the `pr_submit_review` handler to pass the new `repoPath` / `writeOutputFile` / `outputFile` inputs through to `github.submit_review()` and surface the new output fields. |
| `coding-harness/workers/workflows/pr_review.json` | Pass `repoPath` + `writeOutputFile` + `outputFile` into the `pr_submit_review` task; add new fields to `outputParameters`. |
| `coding-harness/workers/workflows/taskdefs/pr_submit_review.json` | No shape change (retryCount stays 0); description updated to mention the comment fallback + file output. |
| `coding-harness/workers/tests/test_github.py` | Unit tests for detection, fallback, and file output (see `testing.md`). |

## Shared contracts

These names/signatures are the single source of truth; every component reuses
them verbatim.

### `common/github.py`

```python
# Cached authenticated gh login for this process ("" if unauthenticated).
def authenticated_login() -> str: ...

# The PR author's login, or "" if it can't be read.
def pr_author(repo_or_url: str, number: int) -> str: ...

# Render the structured review into a single markdown blob (summary + folded
# inline findings). Used by BOTH the comment fallback and the file output so
# they are always identical.
def render_review_markdown(summary: str, comments: list | None) -> str: ...

# Write `content` to `output_file` (relative paths resolve under repo_path).
# Creates parent dirs. Returns the absolute path written, or "" on failure
# (never raises — file output is best-effort and must not fail the review).
def write_review_file(content: str, output_file: str,
                      repo_path: str | None = None) -> str: ...
```

`submit_review` gains parameters and richer output — signature:

```python
def submit_review(repo_or_url: str, number: int, *, summary: str,
                  event: str = "COMMENT", comments: list | None = None,
                  repo_path: str | None = None,
                  write_output_file: bool = False,
                  output_file: str = ".conductor/review-output.md") -> dict: ...
```

Decision order inside `submit_review`:

1. Compute `body = summary or "Automated review."` and
   `md = render_review_markdown(summary, comments)`.
2. If `write_output_file`, call `write_review_file(md, output_file, repo_path)`
   and record `outputFile` (best-effort, before any posting).
3. `self_review = bool(login) and bool(author) and login.casefold() == author.casefold()`
   where `login = authenticated_login()`, `author = pr_author(...)`.
4. If `self_review` → post via comment fallback (`mode="comment"`).
5. Else try the reviews API (existing inline-then-summary-only logic,
   `mode="review"`). If it raises with a 422 that looks like self-author
   (message contains `422` / `"Unprocessable"` / `"author"`), fall back to the
   comment path (`mode="comment"`) rather than re-raising.

The comment fallback posts `render_review_markdown(...)` through the same code
path as `pr_comment` (issue-comments API, `HARNESS_MARKER` appended) so the
review-feedback loop stays re-runnable.

### `submit_review` return dict (superset — existing keys unchanged)

```jsonc
{
  "reviewed": true,
  "event": "COMMENT" | "REQUEST_CHANGES",
  "inlineCount": 0,          // inline comments actually posted (0 in comment mode)
  "inline": false,           // true only when inline review comments landed
  "url": "<html_url or comment url>",
  "mode": "review" | "comment",   // NEW: which API was used
  "selfReview": true,             // NEW: reviewer == author was detected
  "outputFile": "/abs/path/.conductor/review-output.md"  // NEW: "" if not written
}
```

### Task inputs (`pr_submit_review`)

Existing: `repo` (or `repoUrl`), `number`, `structured`. **New (all optional):**

| Input | Type | Default | Meaning |
| --- | --- | --- | --- |
| `repoPath` | string | — | Local checkout; base dir for the output file. |
| `writeOutputFile` | bool | `false` | Also write the review to `outputFile`. |
| `outputFile` | string | `.conductor/review-output.md` | Path (relative → under `repoPath`). |

### Naming conventions

- `mode` values are lowercase `"review"` / `"comment"`.
- Login comparison is case-insensitive (`str.casefold()`), both sides trimmed.
- The default review-output path is the literal string
  `.conductor/review-output.md` — reused verbatim everywhere.
- File output is **best-effort**: a write failure logs a warning and yields
  `outputFile: ""`; it never fails the task or blocks posting.
