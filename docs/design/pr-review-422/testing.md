# pr_review self-review fix — Testing

Reuses contracts from [architecture.md](architecture.md). Tests follow the
existing `gh`-mocking style in `workers/tests/test_github.py` (patch
`common.github.run` / `_gh` / `pr_comment` and assert on the dispatched
argv + returned dict).

## `common/github.py`

1. **Self-review → comments mode.** `viewer_login()` and `pr_author_login()`
   return the *same* login. `submit_review(...)` must NOT hit the reviews API;
   result has `mode == "comments"`, `selfReview is True`, and `pr_comment`
   (conversation summary) was called. Verify inline findings are posted via
   `POST .../pulls/<n>/comments` when a `headRefOid` is present.

2. **Different users → review mode (regression).** viewer != author →
   reviews API is called exactly as today; `mode == "review"`,
   `selfReview is False`. Existing inline-drop 422 fallback still works.

3. **Reviews-API self-review 422 fallback.** viewer/author lookup returns None
   (undetectable), the reviews `POST` raises a 422 whose text matches
   `SELF_REVIEW_422_HINTS` → falls back to `post_review_comments`,
   `mode == "comments"`, `selfReview is True`.

4. **Non-self-review 422 unchanged.** A line-not-in-diff 422 (text NOT matching
   the hints) still triggers the summary-only inline-drop retry, `mode ==
   "review"`.

5. **Local file written.** With `local_output_path` set (relative + `repo_path`
   given, and absolute), `render_review_markdown(...)` content is written to the
   resolved path, parent dirs created, and `localOutputPath` in the result equals
   the absolute path. With the option off, no file is written and
   `localOutputPath == ""`. A write failure is logged, not raised, and posting
   still proceeds.

6. **`render_review_markdown`.** Snapshot the format: verdict badge, summary,
   `### Inline findings` bullets, trailing `HARNESS_MARKER`; findings section
   omitted when `comments` is empty; unanchorable (no `line`) rendered as
   `` `path` — body``.

7. **`viewer_login` caching + safety.** Second call issues no extra `gh` call;
   a non-zero `gh api user` returns `None` without raising.

## `gitops/tasks.py`

8. **`pr_submit_review` passthrough.** Patch `github.submit_review`; assert
   `reviewer`, `repo_path`, and the computed `local_output_path` (None when
   `writeLocalFile` is false, else `localOutputPath`) are forwarded, and the log
   line includes `mode=` and `file=`.

## Workflow

9. **`pr_review.json` shape.** Parse the JSON: `writeReviewFile` /
   `reviewOutputPath` present in `inputParameters` + `inputTemplate`; `submit`
   task forwards `repoPath`, `writeLocalFile`, `localOutputPath`;
   `outputParameters` include `mode`, `selfReview`, `localOutputPath`. (Extends
   the existing workflow-JSON validation test.)

## Manual / integration

- Against a real repo, open a PR as the bot account and run `pr_review`: confirm
  it no longer 422-FAILs, that a conversation comment (+ inline comments where
  lines anchor) lands, and that `.conductor/review-output.md` exists in the
  checkout with the rendered review.
