# code-parallel

Investigate the way GitHub issues are pulled in the issue_to_pr workflow harness code. Specifically: find where the issue body/description is fetched from the GitHub API, trace how that text is passed to the OpenSpec planner (openspec new change), and identify any truncation, escaping, or length-limiting that could cause the description to be cut off before it is fully passed to the planner. Produce a written report of your findings (e.g. in a markdown file or as code comments) — do not make any code changes.
