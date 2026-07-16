# pr_review self-review 422 fix — Architecture

## Problem

`pr_review` ends by calling `pr_submit_review`, which posts a **formal GitHub
review** via `POST repos/{slug}/pulls/{n}/reviews`. GitHub **forbids reviewing
your own PR**: when the authenticated `gh` user (the reviewer) is the PR author,
the reviews API returns **HTTP 422** (`"Review cannot be submitted…"` /
`"Can not approve/request changes on your own pull request"`). The whole task
then FAILs.

Today `submit_review` only retries by dropping inline comments (its fallback for
*line-not-in-diff* 422s). That does not help a self-review 422 — the reviews API
rejects the request regardless of inline comments.

## Fix (three parts)

1. **Detect self-review** — compare the authenticated viewer login against the
   PR author login before touching the reviews API.
2. **Fall back to individual PR comments** — when reviewer == author (or when the
   reviews API returns a self-review 422 anyway), post the findings through the
   **comments API** (inline review comments per finding + one conversation
   summary comment) instead of a formal review submission.
3. **Optional local file** — write the rendered review markdown to a local file
   (default `.conductor/review-output.md` under the checkout) so the output is
   never lost, regardless of which posting path is taken.

## Tech stack

Unchanged: Python 3, Netflix Conductor `@worker_task`, the `gh` CLI shelled
through `common/exec.run`. No new dependencies. All GitHub logic stays in
`common/github.py`; the worker layer (`gitops/tasks.py`) stays a thin wrapper.

## Module / file layout

Only **existing** files change — no new modules.

| File | Change |
|------|--------|
| `coding-harness/workers/common/github.py` | Add `viewer_login()`, `pr_author_login()`, `render_review_markdown()`, `post_review_comments()`; rewrite `submit_review()` to detect self-review, fall back to comments, and optionally write a local file. |
| `coding-harness/workers/gitops/tasks.py` | Extend `pr_submit_review` worker to read the new inputs (`reviewer`, `localOutputPath`, `writeLocalFile`, `repoPath`) and pass them through; extend the log line with `mode`. |
| `coding-harness/workers/workflows/pr_review.json` | Add workflow inputs `writeReviewFile`, `reviewOutputPath`; pass `repoPath` + local-file options into the `submit` task; add `mode`, `selfReview`, `localOutputPath` to `outputParameters`. |
| `coding-harness/workers/tests/test_github.py` | Add self-review detection + comments-fallback + local-file tests. |
| `coding-harness/workers/tests/test_gitops.py` | Add `pr_submit_review` passthrough test for the new inputs. |

## Shared contracts

These names/types are the single source of truth; every component below reuses
them verbatim.

### Constants (in `common/github.py`)

```python
HARNESS_MARKER = "<!-- conductor-harness -->"          # existing, reused
DEFAULT_REVIEW_OUTPUT_PATH = ".conductor/review-output.md"
# gh/GitHub 422 substrings that mean "you can't review your own PR"
SELF_REVIEW_422_HINTS = ("own pull request", "review cannot be submitted")
```

### Helpers (new, in `common/github.py`)

```python
def viewer_login() -> str | None:
    """Login of the authenticated gh user (`gh api user --jq .login`).
    Cached per process; None if gh is unauthenticated. Never raises."""

def pr_author_login(repo_or_url: str, number: int) -> str | None:
    """PR author's login (`gh pr view --json author`). None on error."""

def render_review_markdown(summary: str, verdict: str,
                           comments: list | None) -> str:
    """Render the full review as one markdown blob: an H2 title with the
    verdict badge, the summary, then an '### Inline findings' bullet list of
    `path:line — body` for each anchorable comment. Reused for both the
    fallback conversation comment body and the local file."""

def post_review_comments(repo_or_url: str, number: int, *, summary: str,
                         verdict: str, comments: list | None) -> dict:
    """Post the review through the COMMENTS API (no formal review):
    for each anchorable finding, POST an inline review comment to
    `repos/{slug}/pulls/{n}/comments` (commit_id = head sha, side=RIGHT);
    un-anchorable findings are folded into a single conversation summary
    comment posted via `pr_comment`. Returns the comments-mode result dict."""
```

### `submit_review` — new signature

```python
def submit_review(repo_or_url: str, number: int, *, summary: str,
                  event: str = "COMMENT", comments: list | None = None,
                  reviewer: str | None = None,
                  local_output_path: str | None = None,
                  repo_path: str | None = None) -> dict:
```

- `reviewer` — optional override; when None, resolved via `viewer_login()`.
- `local_output_path` — when set, `render_review_markdown(...)` is written there
  before any posting (path is resolved relative to `repo_path` if given).
- `repo_path` — local checkout root, used to resolve a relative
  `local_output_path`.

### Decision + posting logic (order of operations in `submit_review`)

1. `md = render_review_markdown(summary, verdict_from_event, comments)`.
2. If `local_output_path`: write `md` to it (mkdir -p parent). Records
   `localOutputPath` (absolute) in the result; a write error is logged, not
   raised.
3. `self_review = reviewer_login and reviewer_login == pr_author_login(...)`
   (compared case-insensitively, both sides trimmed).
4. If `self_review`: return `post_review_comments(...)` result with
   `selfReview=True`, `mode="comments"`.
5. Else attempt the reviews API (existing path, incl. the inline-drop retry).
   If it still raises with a `SELF_REVIEW_422_HINTS` match, fall back to
   `post_review_comments(...)` (`selfReview=True`, `mode="comments"`).

### Result dict (returned by both paths — superset of today's)

```python
{
  "reviewed": bool,          # True if anything was posted
  "mode": str,               # "review" | "comments"
  "selfReview": bool,        # reviewer == author (or 422 self-review detected)
  "event": str,              # "COMMENT" | "REQUEST_CHANGES"
  "inlineCount": int,        # inline comments actually posted
  "inline": bool,            # True if inline comments landed
  "url": str,                # review html_url, or summary-comment url in comments mode
  "localOutputPath": str,    # absolute path written, or "" if not written
}
```

`mode`, `selfReview`, and `localOutputPath` are **new**; existing keys keep their
meaning so nothing downstream breaks.

### Naming conventions

- Python: `snake_case` functions, keyword-only options after `*`, plain-dict
  returns; worker tasks wrap with `common.results.ok/fail`.
- Conductor JSON: `camelCase` keys (`writeReviewFile`, `reviewOutputPath`,
  `localOutputPath`, `selfReview`).
- Every harness-posted comment (conversation + inline) carries `HARNESS_MARKER`
  so `pr_comments` skips it — preserving the re-runnable feedback loop.

See [data-model.md](data-model.md) for exact shapes and the markdown format, and
[testing.md](testing.md) for the test plan.
