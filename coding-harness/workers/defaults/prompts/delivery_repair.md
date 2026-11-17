This delivery is NOT ACCEPTED yet. Planned paths that do not exist in the worktree and MUST be created, with real content: {{missingPathsJson}}
Rejected paths (correct them without fabricating replacements): {{rejectedPathsJson}}
Command failures, INCLUDING THE FROZEN CHECKS the harness ran on the host against your worktree after you finished (a browser scenario cannot launch inside your sandbox, so this output is the only way you see it): {{commandFailuresJson}}
Sandbox denials: {{sandboxDenialsJson}}

Repair exactly this, within the authorized tools: create every missing path, and change the APPLICATION until every failed frozen check passes (read its script under .cc/checks/ to see exactly what it drives and asserts; the script is frozen, never edit it). Do not infer success from the prior prose report; do not stop because the files that already exist look complete, or because tests outside this subtask fail: the delivery is accepted only when every missing path exists and every frozen check passes.

Subtask: {{prompt}}

Previous structured agent report: {{previousStructuredReportJson}}
Previous status: {{status}}
Previous error: {{error}}
