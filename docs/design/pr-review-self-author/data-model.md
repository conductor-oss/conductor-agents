# pr_review self-author fallback — Data model

Reuses names/signatures from [architecture.md](./architecture.md) verbatim.

## ReviewResult

Returned by both `submit_review()` and `post_review_as_comments()`. A superset of
today's shape — existing keys keep their meaning; new keys are additive so the current
workflow output mapping keeps working.

| Key | Type | Meaning |
| --- | --- | --- |
| `reviewed` | `bool` | The review landed somewhere (GitHub and/or file). |
| `mode` | `str` | `"review"` (formal reviews API) or `"comments"` (comments-API fallback). **New.** |
| `reason` | `str` | Why the fallback ran: `""`, `"self-author"`, or `"422-fallback"`. **New.** |
| `event` | `str` | `COMMENT` or `REQUEST_CHANGES` (never `APPROVE`). |
| `inlineCount` | `int` | Inline comments actually posted. |
| `inline` | `bool` | Whether any inline comments were posted (vs folded into the body). |
| `url` | `str` | `html_url` of the formal review, or of the issue comment in comments mode; `""` if none. |
| `fileWritten` | `bool` | Local output file was written. **New.** |
| `outputPath` | `str` | Absolute path written, or `""` (including when `output_file=""`). **New.** |

Legacy consumers reading `event` / `inlineCount` / `inline` / `url` are unaffected.

## pr_comments() output — added key

`github.pr_comments()` gains one field (sourced from `gh pr view --json author`, so
`_PR_META_FIELDS` adds `author`):

| Key | Type | Meaning |
| --- | --- | --- |
| `author` | `str` | PR author login, or `""`. **New.** |

The `pr_review.json` workflow wires `fb.output.author` into the `submit` task's
`author` input, avoiding a second `gh pr view` call inside `submit_review`.

## Task I/O — `pr_submit_review`

Inputs (camelCase, per convention):

| Input | Type | Default | Notes |
| --- | --- | --- | --- |
| `repo` / `repoUrl` | `str` | — | Existing. |
| `number` | `int` | — | Existing. |
| `structured` | `dict` \| JSON `str` | `{}` | Existing `{summary, verdict, comments[]}`; fed by `final_review.output.result`. |
| `author` | `str` | `""` | PR author login from `fb.output.author`. **New.** |
| `outputFile` | `str` | `""` (task) / `REVIEW_OUTPUT_DEFAULT` (workflow template) | Local path; `""` disables file output. **New.** |
| `repoPath` | `str` | `""` | Base for resolving a relative `outputFile`; wired from `${clone.output.repoPath}`. **New.** |

`verdict` → `event` mapping is unchanged: `request_changes` → `REQUEST_CHANGES`,
everything else → `COMMENT`.

The task maps `outputFile`→`output_file`, `repoPath`→`repo_path`, `author`→`author`, and
calls `github.submit_review(...)`. Output is the `ReviewResult` above, placed on `task`
output by `ok(...)`.

### `submit` log line

The existing `ok(task, out, [...])` call (`gitops/tasks.py:281`) keeps its current
fields and **appends** the two new ones — no field is removed or reordered:

```python
return ok(task, out, [f"[pr_submit_review] #{i.get('number')} event={out['event']} "
                      f"inline={out['inlineCount']} (posted inline={out['inline']}) "
                      f"mode={out['mode']} reason={out['reason']}"])
```

## Workflow changes — `pr_review.json`

### `inputParameters` / `inputTemplate`

- Add `"outputFile"` to `inputParameters`.
- Add to `inputTemplate`: `"outputFile": ".conductor/review-output.md"` — this literal
  **must equal `REVIEW_OUTPUT_DEFAULT`**. It is the single point where the on-by-default
  behavior is applied; users pass `outputFile: ""` to disable.

### `submit` task `inputParameters`

The `submit` task gains three wired inputs (in addition to the existing `repo`,
`number`, `structured`):

```json
"author":     "${fb.output.author}",
"outputFile": "${workflow.input.outputFile}",
"repoPath":   "${clone.output.repoPath}"
```

`repoPath` wiring is **required**: without it the relative default resolves against the
worker cwd, so the file would not land in the checkout.

### `outputParameters`

Existing outputs (`event`, `inlineCount`, `postedInline`, `reviewUrl`, …) stay. Add:

```json
"reviewMode":   "${submit.output.mode}",
"reviewReason": "${submit.output.reason}",
"reviewFile":   "${submit.output.outputPath}"
```

The `final_review` `JSON_JQ_TRANSFORM` and its `queryExpression` are **unchanged**;
`final_review.output.result` continues to feed `submit.structured`.

## Rendered markdown (`render_review_markdown`)

Deterministic layout so the GitHub body and on-disk file are identical:

```
> **Review verdict: REQUEST_CHANGES**   <!-- or COMMENT -->

<summary markdown>

---
### Findings
- `path/to/file.py:42` _(blocking)_ — <body>
- `other.py:10` _(nit)_ — <body>
```

- The `severity` italic is omitted when a comment has no `severity`.
- When `comments` is empty the `Findings` section is omitted entirely.
- The comments-fallback issue comment prepends `HARNESS_MARKER`; the file output does
  not (local files aren't scanned by the feedback loop).
