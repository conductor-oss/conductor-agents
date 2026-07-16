# pr_review self-author 422 fix — Testing

Reuses names from [architecture.md](./architecture.md). GitHub-layer tests live
in `coding-harness/workers/tests/test_github.py`; the task passthrough test lives
in `test_gitops.py`. Both follow the existing pattern: monkeypatch
`common.github.run` with a fake that dispatches on the `gh` argv and records the
payloads posted, so no network or real `gh` is needed.

## Fake gh harness

A `FakeGh` helper maps argv prefixes → canned `Result(code, stdout, stderr)`:

- `gh api user …` → `{"login": "<viewer>"}`
- `gh pr view … --json author` → `{"author": {"login": "<author>"}}`
- `POST .../pulls/{n}/reviews` → configurable: success `{"html_url": …}` **or** a
  422 (non-zero result / `RuntimeError`) to exercise the fallback.
- `gh pr comment` / `POST .../issues/{n}/comments` → success with `html_url`.
- `POST .../pulls/{n}/comments` (inline) → per-call success/failure list.

## Unit tests (`test_github.py`)

1. **viewer_login / pr_author_login** — parse login from JSON; return `""` on
   non-zero exit or malformed JSON (never raise).
2. **self-author detected → comments mode** — viewer == author. Assert **no**
   POST to `/reviews`; an issue comment is posted (prefixed with
   `HARNESS_MARKER`); result `mode="comments"`, `reason="self-author"`,
   `reviewed=True`.
3. **distinct users → formal review** — viewer != author, reviews POST succeeds.
   Assert `/reviews` called; `mode="review"`, `reason=""`, `inline=True`.
4. **422 fallback for distinct users** — viewer != author but reviews POST
   returns 422. Assert it then routes to the comments API; `mode="comments"`,
   `reason="422-fallback"`.
5. **case-insensitive login match** — `Alice` vs `alice` → treated as
   self-author.
6. **empty login is not self-author** — viewer `""` (auth probe failed) →
   attempts the formal review path, not the comments fallback.
7. **inline comment resilience** — 3 findings where the 2nd inline POST fails;
   assert the other 2 land, `inlineCount == 2`, and the batch does not raise.
8. **local file output (relative + repo_path)** —
   `output_file=REVIEW_OUTPUT_DEFAULT`, `repo_path=tmp`. Assert the file exists at
   `tmp/.conductor/review-output.md`, content equals
   `render_review_markdown(...)`, `fileWritten=True`, `outputPath` is the
   absolute path under `tmp`. This is the automated form of the "written in the
   checkout" smoke check.
9. **empty output_file writes nothing** — `submit_review(..., output_file="")`:
   assert no file is created anywhere, `fileWritten=False`, `outputPath=""`, and
   posting still proceeds. Locks the library-level opt-in default.
10. **file write failure is swallowed** — `output_file` under an
    unwritable/non-dir path → `fileWritten=False`, `outputPath=""`, no exception,
    review still posts.
11. **render_review_markdown shape** — verdict banner present; `Findings` omitted
    when `comments` empty; severity italic omitted when absent; ordering stable.
12. **pr_comments returns author** — with `author` in `_PR_META_FIELDS`, assert
    the returned dict includes `author`.

## Task-level test (`test_gitops.py`)

`pr_submit_review`: feed `task.input_data` with `structured` as a JSON **string**,
plus `author`, `outputFile`, and `repoPath`. Assert:

- camelCase→snake_case mapping reaches `github.submit_review`
  (`outputFile→output_file`, `repoPath→repo_path`, `author→author`) — via a
  monkeypatched `submit_review` recording its kwargs;
- the log line includes `mode=` and `reason=` (matching the extended `ok(...)`
  call);
- `verdict:"request_changes"` → `event="REQUEST_CHANGES"`.

## Workflow default test

Assert `pr_review.json`'s `inputTemplate["outputFile"]` equals
`.conductor/review-output.md` (the `REVIEW_OUTPUT_DEFAULT` value) and that the
`submit` task `inputParameters` wire `author`, `outputFile`, and
`repoPath: "${clone.output.repoPath}"`. Guards the single-point-of-substitution
and the repoPath wiring so the two coders (github.py vs workflow) cannot diverge.
May live in `test_workflows.py` if workflow-JSON assertions are covered there.

## Manual / smoke check

Not automated: run `pr_review` against a PR opened by the same account the
harness's `gh` is authenticated as, and confirm (a) no 422 surfaces, (b) findings
appear as PR comments, and (c) `.conductor/review-output.md` is written in the
checkout (supplied by the workflow `inputTemplate` default + `repoPath` wiring).

## Non-goals

- No change to the reviewer prompt, diff computation, the `final_review`
  JSON_JQ_TRANSFORM, or the `approve_gate`/HUMAN task.
- No de-duplication of comments across re-runs beyond the existing
  `HARNESS_MARKER` skip in `pr_comments()`.
