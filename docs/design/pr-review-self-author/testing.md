# pr_review self-author fallback — Testing

Reuses names from [architecture.md](./architecture.md). All tests live in
`coding-harness/workers/tests/test_github.py` and follow the existing pattern there:
monkeypatch `common.github.run` with a fake that dispatches on the `gh` argv and records
the payloads posted, so no network or real `gh` is needed.

## Fake gh harness

A `FakeGh` helper maps argv prefixes → canned `Result(code, stdout, stderr)`:

- `gh api user …` → `{"login": "<viewer>"}`
- `gh pr view … --json author` → `{"author": {"login": "<author>"}}`
- `POST .../pulls/{n}/reviews` → configurable: success `{"html_url": …}` **or** a 422
  `RuntimeError`/non-zero result to exercise the fallback.
- `gh pr comment` / `POST .../issues/{n}/comments` → success with `html_url`.
- `POST .../pulls/{n}/comments` (inline) → per-call success/failure list.

## Unit tests

1. **viewer_login / pr_author_login** — parse login from JSON; return `""` on non-zero
   exit or malformed JSON (never raise).
2. **self-author detected → comments mode** — viewer == author. Assert **no** POST to
   `/reviews`; assert an issue comment posted (prefixed with `HARNESS_MARKER`); result
   `mode="comments"`, `reason="self-author"`, `reviewed=True`.
3. **distinct users → formal review** — viewer != author, reviews POST succeeds. Assert
   `/reviews` called; `mode="review"`, `reason=""`, `inline=True`.
4. **422 fallback for distinct users** — viewer != author but reviews POST returns 422.
   Assert it then routes to the comments API; `mode="comments"`,
   `reason="422-fallback"`.
5. **case-insensitive login match** — `Alice` vs `alice` → treated as self-author.
6. **empty login is not self-author** — viewer `""` (auth probe failed) → attempts the
   formal review path, not the comments fallback.
7. **inline comment resilience** — 3 findings where the 2nd inline POST fails; assert the
   other 2 land, `inlineCount == 2`, and the batch does not raise.
8. **local file output (default on)** — `output_file=".conductor/review-output.md"`,
   `repo_path=tmp`. Assert the file exists under `tmp/.conductor/`, content equals
   `render_review_markdown(...)`, `fileWritten=True`, `outputPath` is absolute and rooted
   at `tmp`.
9. **empty output_file writes nothing** — `output_file=""` → **no** file created,
   `fileWritten=False`, `outputPath=""`, no exception, and the review still posts
   normally. (Pins the library-level disable switch.)
10. **file write failure is swallowed** — `output_file` under an unwritable/again-file
    path → `fileWritten=False`, `outputPath=""`, no exception, review still posts.
11. **repoPath resolution** — relative `output_file` + `repo_path=tmp` lands the file
    inside `tmp` (not the process cwd); relative `output_file` with `repo_path=""`
    resolves against cwd (documents the wiring requirement).
12. **render_review_markdown shape** — verdict banner present; `Findings` omitted when
    `comments` empty; severity italic omitted when absent; ordering stable.
13. **pr_comments returns author** — with `author` in `_PR_META_FIELDS`, assert the
    returned dict includes `author`.

## Task-level test (`test_gitops.py`)

`pr_submit_review`: feed `task.input_data` with `structured` as a JSON **string**,
plus `author`, `outputFile`, and `repoPath`; assert the camelCase→snake_case kwarg
mapping (`outputFile`→`output_file`, `repoPath`→`repo_path`), and that the appended log
line includes `mode=` and `reason=` while preserving the existing `event=`/`inline=`
fields (matching the `ok(...)` shape at `gitops/tasks.py:281`).

## Manual / smoke check

Not automated: run `pr_review` against a PR opened by the same account the harness's `gh`
is authenticated as, and confirm (a) no 422 surfaces, (b) findings appear as PR comments,
and (c) `.conductor/review-output.md` is written **in the checkout** (`clone.output.repoPath`)
because the workflow's `inputTemplate` default equals `REVIEW_OUTPUT_DEFAULT` and
`repoPath` is wired into `submit`.

## Non-goals

- No change to the reviewer prompt, diff computation, the `final_review` JQ transform, or
  the `approve_gate`/HUMAN task.
- No de-duplication of comments across re-runs beyond the existing `HARNESS_MARKER` skip
  in `pr_comments()`.
