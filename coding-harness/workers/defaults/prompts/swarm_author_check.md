You write ONE acceptance check for a swarm that is about to build this request, in this repository (branch `{{branch}}`; do not commit: the harness commits the tree):

{{request}}

The architect decided the check; you write it exactly as decided:
{{check}}

Read the architect's brief `.cc/design.md`, above all THE CONTRACT THE CHECKS USE, and the design documents in `design/`: the check drives the program through exactly those words (labels, routes, commands, response shapes, environment variables, the start command). Other authors are writing the other checks beside you; a shared helper they might also write is not shared: keep your check self-contained in its own file.

Rules for the check:
- It runs from the repository root with the given `command`, exits 0 on success and non-zero with a one-line reason.
- It can never hang: bound every wait (a connection, a read, a reply, the program's start-up) and fail with a reason when the bound passes.
- It pins BEHAVIOUR, never environment: anything it needs from the host (a port, a directory, data) it chooses itself when it runs and hands to the program; it starts the program itself and stops it, leaving nothing running.
- A browser check drives the browser the harness provides through its helper: `const { scenario } = await import(process.env.PLAYWRIGHT_SCENARIO); await scenario(async ({ page }) => { ... }, { start: [...], env: {...}, url });` (it starts the program, waits for `url` to answer over HTTP, opens it, always cleans up). Use roles and labels from the contract; never a `try {} catch {}` that hides an error.

Write the file, then run the command ONCE: it must FAIL now, because nothing is built yet (a check that passes before the work proves nothing). If it passes, it is not checking what the scenario says; fix it. Report the files you wrote and whether it fails now.
