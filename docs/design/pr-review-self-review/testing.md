# pr_review self-review fallback — Testing

Add cases to `coding-harness/workers/tests/test_github.py` (and, if a handler
test is warranted, `test_gitops.py`). Follow the existing style: monkeypatch
`common.github.run` / `_gh` to return canned `run` results, assert on the dict
and on the `gh` argv captured. No network, no real `gh`.

Reset the process caches (`authenticated_login`'s global) between tests via a
fixture so login state doesn't leak across cases.

## Unit tests — `submit_review`

1. **Self-review → comment fallback.** `authenticated_login()` and `pr_author()`
   both return `"botuser"`. Assert: the reviews endpoint is **never** called;
   `gh pr comment` (issue-comments path) is; result `mode == "comment"`,
   `selfReview is True`, `reviewed is True`, `inline is False`, `inlineCount == 0`.

2. **Distinct users → normal review.** login `"botuser"`, author `"someone"`.
   Assert reviews API called, `mode == "review"`, `selfReview is False`; existing
   inline/summary behavior preserved.

3. **422 catch-all fallback.** Distinct users, but the reviews POST raises an
   exception whose message contains `422`/`Unprocessable`. Assert it falls back
   to the comment path (`mode == "comment"`) instead of raising.

4. **Unknown author.** `pr_author()` returns `""` (e.g. `gh` failed). Assert
   `selfReview is False` and the normal review path runs — detection is
   fail-open toward the existing behavior.

5. **Case-insensitive match.** login `"BotUser"`, author `"botuser"` →
   `selfReview is True`.

## Unit tests — file output

6. **write_output_file=True** writes `render_review_markdown(...)` to the given
   path under `tmp_path` (as `repo_path`), creates parent dirs, and sets
   `outputFile` to the absolute path. Content equals the rendered markdown.

7. **Default path** used when `output_file` omitted → `.conductor/review-output.md`
   under `repo_path`.

8. **Best-effort failure.** Make the write raise (unwritable dir); assert
   `outputFile == ""`, no exception propagates, and posting still happens.

9. **File written regardless of post mode.** With `write_output_file=True`,
   assert `outputFile` is set in both the self-review (comment) and distinct-user
   (review) cases.

## Unit tests — `render_review_markdown`

10. Empty `comments` → no `### Inline findings` section.
11. Comments with/without `severity` and with a non-anchorable entry (missing
    `line`) → all appear in the findings list; severity shown only when present.
12. `event`/verdict reflected in the heading (`COMMENT` vs `REQUEST_CHANGES`).

## Helper tests

13. `authenticated_login()` caches: two calls invoke `gh api user` once; returns
    `""` and does not raise when the call errors.
14. `pr_author()` parses `.author.login`; returns `""` on non-zero exit or bad JSON.

## Regression

- Existing `submit_review` inline-then-summary-only 422 retry (bad line anchor,
  distinct users) still lands a review — keep the current test green.
- No change to `pr_submit_review` retryCount (stays `0`; a re-post would
  duplicate).
