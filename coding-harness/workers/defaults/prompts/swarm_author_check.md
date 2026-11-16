You write ONE acceptance check for a swarm that is about to build this request, in this repository (branch `{{branch}}`; do not commit: the harness commits the tree):

{{request}}

The architect decided the check; you write it exactly as decided:
{{check}}

Read the architect's brief `.cc/design.md`, above all THE CONTRACT THE CHECKS USE, and the design documents it names in `design/`, if any; when the check `covers` OpenSpec scenarios, read them in `.cc/openspec/specs/` and assert exactly what each scenario's THEN says: the check drives the program through exactly those words (labels, routes, commands, response shapes, environment variables, the start command). Other authors are writing the other checks beside you; a shared helper they might also write is not shared: keep your check self-contained in its own file.

Rules for the check:
- It runs from the repository root with the given `command`, exits 0 on success and non-zero with a one-line reason.
- It can never hang: bound every wait (a connection, a read, a reply, the program's start-up) and fail with a reason when the bound passes.
- It pins BEHAVIOUR, never environment: anything it needs from the host (a port, a directory, data) it chooses itself when it runs and hands to the program; it starts the program itself and stops it, leaving nothing running.
- PORTS: a free port it asks the OS for at run time (bind port 0), for everything it starts, the program and any server it needs (a database, a reference server). NEVER a fixed or well-known port (6379, 5432, 3306, 8080, the program's default): this host and the checks running beside this one use them, and a check that fails or refuses because "port in use" checks nothing. Never connect to, modify or stop a server it did not start, UNLESS the request names a running server to use (for example "the redis running on the standard port"): then use exactly that one, never stop or reconfigure it, and touch only keys and data the check creates and removes itself.
- A browser check drives the browser the harness provides through its helper: `const { scenario } = await import(process.env.PLAYWRIGHT_SCENARIO); await scenario(async ({ page }) => { ... }, { start: [...], env: {...}, url, context: {...}, setup: async ({ page }) => { ... } });` (it starts the program, waits for `url` to answer over HTTP, opens it, always cleans up). What the browser must provide goes in `context` (Playwright's context options): a location as `{ geolocation: { latitude, longitude }, permissions: ['geolocation'] }`, a `timezoneId`, a `colorScheme`, a `locale`; never replace browser APIs (`navigator.geolocation`, `window.fetch`) with scripts. Answers from outside services are faked with `page.route(...)` INSIDE `setup`, which runs before the page opens (in the body they come too late: the page has already loaded). Use roles and labels from the contract; never a `try {} catch {}` that hides an error.

Write the file, then run the command ONCE: it must FAIL now, because nothing is built yet (a check that passes before the work proves nothing). If it passes, it is not checking what the scenario says; fix it. Report the files you wrote and whether it fails now.
