# pr_review self-review fix — Data model & wiring

Reuses names/types from [architecture.md](architecture.md) verbatim.

## `render_review_markdown` output format

```
## Automated review — <VERDICT BADGE>

<summary markdown, or "No summary provided.">

### Inline findings

- `path:line` — body
- `path` — body            # when line is missing/unanchorable
...

<HARNESS_MARKER>
```

- Verdict badge: `✅ comment` for `COMMENT`, `🔧 request changes` for
  `REQUEST_CHANGES`.
- The `### Inline findings` section is omitted entirely when `comments` is empty.
- `HARNESS_MARKER` is appended so this same blob is safe to post as a comment.
- The **local file** is written with the marker too (harmless in the file).

## `post_review_comments` behavior

Input: `summary`, `verdict`, `comments[]` (same items as `submit_review`).

1. Resolve head sha: `gh pr view <n> --repo <slug> --json headRefOid`
   → `headRefOid`. If unavailable, skip inline posting (fold all findings).
2. For each comment where `path` and integer `line` are present **and** a head
   sha exists: `POST repos/{slug}/pulls/{n}/comments` with
   `{commit_id, path, line, side:"RIGHT", body: body + "\n\n" + HARNESS_MARKER}`.
   Count each success in `inlineCount`. A per-comment failure (line not in diff →
   422) is caught, logged, and that finding is instead folded into the summary.
3. Post one conversation comment via `pr_comment(slug, n, body)` where `body` is
   `render_review_markdown(summary, verdict, folded_comments)` — `folded_comments`
   being the findings that could **not** be posted inline (all of them if step 1
   found no head sha). `pr_comment` already appends `HARNESS_MARKER`.

Return:

```python
{
  "reviewed": True,
  "mode": "comments",
  "selfReview": True,
  "event": event,               # COMMENT | REQUEST_CHANGES (never APPROVE)
  "inlineCount": <#posted inline>,
  "inline": <inlineCount > 0>,
  "url": <summary comment url>,
  "localOutputPath": "",        # filled in by submit_review, not here
}
```

`submit_review` overwrites `localOutputPath` on the returned dict with the path
it actually wrote (or `""`).

## `viewer_login` / `pr_author_login`

| Helper | gh call | Extract | Cache |
|--------|---------|---------|-------|
| `viewer_login()` | `gh api user --jq .login` | trimmed stdout | module-level, per process (like `_AUTH_DONE`) |
| `pr_author_login(repo, n)` | `gh pr view <n> --repo <slug> --json author` | `author.login` | none |

Both return `None` (never raise) on any error, so a lookup failure degrades to
the normal reviews-API path rather than crashing the task.

## Worker task: `pr_submit_review` inputs

Existing: `repo`/`repoUrl`, `number`, `structured`. New optional inputs:

| Input | Type | Default | Meaning |
|-------|------|---------|---------|
| `reviewer` | string | `""` → None | Override reviewer login (else `viewer_login()`). |
| `repoPath` | string | `""` → None | Local checkout root for resolving `localOutputPath`. |
| `writeLocalFile` | bool | `true` | Whether to write the review markdown to disk. |
| `localOutputPath` | string | `DEFAULT_REVIEW_OUTPUT_PATH` | Relative (to `repoPath`) or absolute output path. |

The worker computes `local_output_path = localOutputPath if writeLocalFile else
None` and forwards `reviewer`, `repo_path`, `local_output_path` to
`github.submit_review`. Log line becomes:

```
[pr_submit_review] #<n> mode=<mode> event=<event> self=<selfReview> \
  inline=<inlineCount> (posted inline=<inline>) file=<localOutputPath>
```

## Workflow: `pr_review.json`

New `inputParameters` + `inputTemplate` defaults:

```json
"writeReviewFile": true,
"reviewOutputPath": ".conductor/review-output.md"
```

`submit` task `inputParameters` gains:

```json
"repoPath": "${clone.output.repoPath}",
"writeLocalFile": "${workflow.input.writeReviewFile}",
"localOutputPath": "${workflow.input.reviewOutputPath}"
```

New `outputParameters`:

```json
"mode": "${submit.output.mode}",
"selfReview": "${submit.output.selfReview}",
"localOutputPath": "${submit.output.localOutputPath}"
```

Existing output params (`event`, `inlineCount`, `postedInline`, `reviewUrl`, …)
are unchanged — `reviewUrl` maps to `submit.output.url` in either mode.
