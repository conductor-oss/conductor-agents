## 1. Report scaffold and index

Files: coding-harness/docs/reports/issue-ingestion/README.md
Test: test -f coding-harness/docs/reports/issue-ingestion/README.md && rg -q "issue_to_pr" coding-harness/docs/reports/issue-ingestion/README.md

- [ ] 1.1 Create the `coding-harness/docs/reports/issue-ingestion/` directory and add `README.md` as the report index, stating the investigation scope (GitHub issue body → OpenSpec planner in `issue_to_pr`/`code_parallel`) and the read-only, no-code-changes constraint.
- [ ] 1.2 In `README.md`, summarize the three questions being answered (where the body is fetched, how it is passed to `openspec new change`, what could truncate/escape/length-limit it) and link to the four part files (`01-fetch-and-assembly.md`, `02-planner-wiring.md`, `03-risk-register.md`, `04-recommendations.md`).
- [ ] 1.3 In `README.md`, record the headline finding up front: two independent paths carry the instruction, and path (b) (`goal` → agent prompt) is the load-bearing, full-fidelity channel.

## 2. Fetch-stage and instruction-assembly trace

Files: coding-harness/docs/reports/issue-ingestion/01-fetch-and-assembly.md
Test: test -f coding-harness/docs/reports/issue-ingestion/01-fetch-and-assembly.md && rg -q "github.py:89" coding-harness/docs/reports/issue-ingestion/01-fetch-and-assembly.md && rg -q "issue_to_pr.json:60" coding-harness/docs/reports/issue-ingestion/01-fetch-and-assembly.md

- [ ] 2.1 Document `issue_fetch()` in `common/github.py:89-104`: it runs `gh issue view <n> --repo <slug> --json number,title,body,state,url,labels` and returns `body` verbatim (`d.get("body", "")`) with no length cap. Cite the exact lines.
- [ ] 2.2 Document the `issue_fetch` worker task wrapper in `gitops/tasks.py` (~216-226): confirm the only truncation is cosmetic (`title[:80]`) in the human-readable log line and does not affect the forwarded payload. Cite the exact line.
- [ ] 2.3 Document `issue_to_pr.json:60`: Conductor template substitution inlines `${issue.output.title}` and `${issue.output.body}` into a single `instruction` string; quote the template and mark it as the assembly point.
- [ ] 2.4 Add a hop-by-hop trace table for this stage with columns `file:line`, action, and verdict (verbatim / cosmetic-only / transformed).

## 3. Planner-wiring trace

Files: coding-harness/docs/reports/issue-ingestion/02-planner-wiring.md
Test: test -f coding-harness/docs/reports/issue-ingestion/02-planner-wiring.md && rg -q "openspec_plan.json" coding-harness/docs/reports/issue-ingestion/02-planner-wiring.md && rg -q "openspec_cli.py" coding-harness/docs/reports/issue-ingestion/02-planner-wiring.md

- [ ] 3.1 Trace `code_parallel.json` → `openspec_plan.json`: show `instruction` flows through unchanged and reaches the planner via two independent paths.
- [ ] 3.2 Document path (a): `openspec_plan.json:25` sets `instruction` as `openspec_new_change`'s `description`, becoming `openspec new change <name> --description <body>` — one CLI argument — via `openspecops/tasks.py:23` → `common/openspec_cli.py:43-47`. Cite the exact lines.
- [ ] 3.3 Document path (b): `openspec_plan.json:41` passes `instruction` as `goal` into the artifact-drain loop; `openspec_generate_artifact.json:28` inlines `goal` directly into each artifact-writer agent's prompt — the text that actually drives content generation. Cite the exact lines.
- [ ] 3.4 Document that `--description` argv is passed through `common/exec.py`'s `subprocess.run` with an argv list and no `shell=True` (`common/exec.py:28-31`), so there is no shell-quoting stage that could mangle or truncate the body. Cite the exact lines.
- [ ] 3.5 State the central finding: path (b) is authoritative/full-fidelity; any truncation the downstream `openspec new change --description` applies is bypassed for actual content generation. Add a hop-by-hop trace table for this stage.

## 4. Loss-point risk register

Files: coding-harness/docs/reports/issue-ingestion/03-risk-register.md
Test: test -f coding-harness/docs/reports/issue-ingestion/03-risk-register.md && rg -q "possible-silent" coding-harness/docs/reports/issue-ingestion/03-risk-register.md && rg -q "fail-hard" coding-harness/docs/reports/issue-ingestion/03-risk-register.md

- [ ] 4.1 Build a risk-register table with columns: loss point, `file:line` (where relevant), classification (confirmed hard cap / possible-silent / fail-hard-not-silent), and impact given the two-path architecture.
- [ ] 4.2 Register the `openspec new change --description` CLI possibly treating the body as a short summary/first line as *possible-silent* on path (a), and note it is bypassed by path (b) — lower operational severity than it first appears.
- [ ] 4.3 Register Conductor `${...}`/dollar-brace re-interpolation inside the issue body and JSON-string escaping of quotes/backslashes/newlines during JQ/template substitution as *possible-silent* (behavior owned by Conductor internals, not provable by static reading).
- [ ] 4.4 Register OS `ARG_MAX` / per-arg `MAX_ARG_STRLEN` on the single `--description <body>` argument and Conductor task input/output payload-size limits as *fail-hard-not-silent* (E2BIG / loud rejection surfaced as `RunError`), i.e. detectable rather than silently corrupting.

## 5. Recommendations, verification plan, and spec alignment

Files: coding-harness/docs/reports/issue-ingestion/04-recommendations.md
Test: test -f coding-harness/docs/reports/issue-ingestion/04-recommendations.md && rg -q "harness-issue-ingestion" coding-harness/docs/reports/issue-ingestion/04-recommendations.md && rg -q "round-trip" coding-harness/docs/reports/issue-ingestion/04-recommendations.md

- [ ] 5.1 Recommend a black-box test of `openspec new change --description <body>` to confirm whether the full body persists into `proposal.md` or is treated as a one-line summary.
- [ ] 5.2 Recommend a live round-trip test with a crafted issue body (containing `${...}`, quotes, backslashes, newlines, and a very large body) to confirm Conductor interpolation and JSON escaping round-trip losslessly and to find the payload-size/`ARG_MAX` trigger point.
- [ ] 5.3 Recommend evaluating a hardened ingestion path (pass large bodies via stdin/file to `openspec` and to the agent prompt) to sidestep arg-length limits, and scope it as a follow-up change (no code changes here).
- [ ] 5.4 Cross-check the report's findings against the `harness-issue-ingestion` spec (`specs/harness-issue-ingestion/spec.md`) and confirm the report satisfies every requirement scenario; note any gaps for follow-up.
