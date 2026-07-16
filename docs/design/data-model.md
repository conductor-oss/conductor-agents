# pr_review self-author 422 fix — Data model

Reuses names/signatures from [architecture.md](./architecture.md) verbatim.

## ReviewResult

Returned by both `submit_review()` and `post_review_as_comments()`. A superset of
today's shape — existing keys keep their meaning; new keys are additive so the
current workflow output mapping keeps working.

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
| `outputPath` | `str` | Absolute path written, or `""`. **New.** |

Legacy consumers reading `event` / `inlineCount` / `inline` / `url` are
unaffected.

## `pr_comments()` output — added key

`github.pr_comments()` gains one field (sourced from `gh pr view --json author`,
so `_PR_META_FIELDS` adds `author`):

| Key | Type | Meaning |
| --- | --- | --- |
| `author` | `str` | PR author login, or `""`. **New.** |

`pr_review.json` wires `fb.output.author` into the `submit` task's `author`
input, avoiding a second `gh pr view` call inside `submit_review`.

## Task I/O — `pr_submit_review`

Inputs (camelCase, per convention):

| Input | Type | Default | Notes |
| --- | --- | --- | --- |
| `repo` / `repoUrl` | `str` | — | Existing. |
| `number` | `int` | — | Existing. |
| `structured` | `dict` \| JSON `str` | `{}` | Existing `{summary, verdict, comments[]}`. |
| `author` | `str` | `""` | PR author login from `fb.output.author`. **New.** |
| `outputFile` | `str` | `""` (task-level) | Local path; **empty disables file output**. The `pr_review.json` workflow overrides this default to `REVIEW_OUTPUT_DEFAULT`. **New.** |
| `repoPath` | `str` | `""` | Base for resolving a relative `outputFile`; wired from `${clone.output.repoPath}`. **New.** |

Kwarg mapping in `pr_submit_review`: `author→author`, `outputFile→output_file`,
`repoPath→repo_path`. `verdict → event` mapping is unchanged
(`request_changes → REQUEST_CHANGES`, everything else → `COMMENT`).

Output is the `ReviewResult` above, placed on `task` output by `ok(...)`.

## Workflow changes (`pr_review.json`)

**`inputParameters`** — add `outputFile`.

**`inputTemplate`** — add `"outputFile": ".conductor/review-output.md"` (the
literal value of `REVIEW_OUTPUT_DEFAULT`; this is the single point where the
default is substituted). Callers pass `"outputFile": ""` to disable file output.

**`submit` task `inputParameters`** — currently only `repo`, `number`,
`structured`. Add three wires:

```json
"author":    "${fb.output.author}",
"outputFile":"${workflow.input.outputFile}",
"repoPath":  "${clone.output.repoPath}"
```

The `repoPath` wire is mandatory: without it the relative `outputFile` resolves
against the worker cwd instead of the checkout, and the smoke check ("written in
the checkout") fails.

**`final_review` (JSON_JQ_TRANSFORM)** — **unchanged.** Its `queryExpression`
still selects `.gated` vs `.auto`, and `submit.inputParameters.structured` still
reads `${final_review.output.result}`.

**`outputParameters`** — existing (`event`, `inlineCount`, `postedInline`,
`reviewUrl`, …) stay. Add:

```json
"reviewMode":   "${submit.output.mode}",
"reviewReason": "${submit.output.reason}",
"reviewFile":   "${submit.output.outputPath}"
```

## `submit` log line (`gitops/tasks.py`)

Today (`gitops/tasks.py:281`):

```python
return ok(task, out, [f"[pr_submit_review] #{i.get('number')} event={out['event']} "
                      f"inline={out['inlineCount']} (posted inline={out['inline']})"])
```

Extend the same single `ok(...)` call — append the two new fields, matching the
existing `out[...]` field style:

```python
return ok(task, out, [f"[pr_submit_review] #{i.get('number')} event={out['event']} "
                      f"mode={out['mode']} reason={out['reason']} "
                      f"inline={out['inlineCount']} (posted inline={out['inline']})"])
```

`out['mode']` and `out['reason']` are always present on the `ReviewResult`, so
this is a straight interpolation with no new failure mode.

## Rendered markdown (`render_review_markdown`)

Deterministic layout so GitHub body and on-disk file are identical:

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
- The comments-fallback issue comment prepends `HARNESS_MARKER`; the file output
  does **not** (local files aren't scanned by the feedback loop).
