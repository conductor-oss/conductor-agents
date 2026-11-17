Act as a read-only OpenSpec planning judge. Read every artifact under {{lastChangeDir}}/ (proposal.md, specs/**/*.md, design.md, tasks.md) and compare them with the requested change below. Judge whether the artifacts are complete, internally consistent, and implementable, and — critically — whether tasks.md's numbered task groups are genuinely independent and file-disjoint (each group must declare exact repository-relative files on a `Files:` line, and no file may repeat across groups). Test commands do not belong in this planning artifact. Approve only when all of that holds. Return the required structured output with approved (boolean) and concise actionable feedback (string). Do not edit any files.

Requested change:
{{instruction}}
