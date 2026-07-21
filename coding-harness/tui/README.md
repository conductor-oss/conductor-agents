# Harness TUI

The dashboard exposes **Automations** (`s`) for the GitHub sweep schedules and a global
**Approval Inbox** (`a`). The inbox refreshes every five seconds on every screen, signals the
execution that owns each WAIT (including nested workflows), excludes timed WAITs, and warns on
legacy HUMAN tasks. Notifications are active only while the TUI process runs.

A terminal interface for driving the Conductor coding harness: **chat with an agent that
runs the harness for you**, or kick off / watch / manage runs by hand — without writing
JSON or leaving the terminal.

It's a **read-mostly client of the Conductor REST API** (the same one the workers and the
`conductor` CLI use). It starts / terminates / retries workflows and renders their live
state; it changes no worker, workflow, or server code. Design docs:
[`../docs/TUI_UX.md`](../docs/TUI_UX.md) (UX) and
[`../docs/TUI_IMPLEMENTATION.md`](../docs/TUI_IMPLEMENTATION.md) (build spec).

## Install & run

```bash
# install (from the tui/ dir)
cd tui && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cd ..

# run FROM THE REPO ROOT (python -m tui needs the repo root on sys.path)
CONDUCTOR_SERVER_URL=http://localhost:8080/api tui/.venv/bin/python -m tui
```

For an authenticated Conductor server, export both `CONDUCTOR_AUTH_KEY` and
`CONDUCTOR_AUTH_SECRET` in the TUI process. The TUI exchanges them for a short-lived API token,
authenticates every REST call (including `/register`), and refreshes an expired token once. A
partial key/secret pair fails fast, and credential values are never displayed.

When launched through `./run.sh tui`, these variables and `CONDUCTOR_SERVER_URL` are also used by
the bootstrap CLI and inherited by registration and workers. A reachable 401/403 is reported as
an authentication/authorization error; it never triggers a local OSS server. If an authenticated
or explicitly Enterprise endpoint is offline, startup fails instead of replacing it with OSS.

Flags: `--model M` (chat driver; default `sonnet`; aliases `sonnet`/`opus`/`haiku` or a
full id like `claude-sonnet-4-6`), `--resume [id]` (continue a past chat; bare = last),
`--dashboard` (land on the dashboard instead of chat), `--server URL`, `--no-notify`,
`--no-workers` (use an externally managed worker fleet instead of starting local workers).
Requires Python 3.12+, a reachable Conductor server, **`ANTHROPIC_API_KEY`** for chat, and
— for the GitHub workflows — the same `gh` auth the workers use. Min terminal size ~80×24.

By default the TUI starts and supervises the local harness workers, forwarding its selected
Conductor server and the `CONDUCTOR_AUTH_KEY` / `CONDUCTOR_AUTH_SECRET` pair. Workers stop with
the TUI; their output is written to `~/.conductor-harness/workers.log`. `./run.sh tui` also
registers the current workflows/task definitions and runs the SIMPLE-task worker gate before
launch, preventing stale server-side contracts. Use `--no-workers` only when workers are already
managed elsewhere.

## Chat (default screen)

The TUI opens into **chat**: an LLM (the `--model`) interprets what you ask and drives the
harness by calling tools — `list_runs`, `get_run`, `start_workflow`, `terminate_run`,
`retry_run`, plus `gh` issue/PR lookup. Try *"review PR 4 on conductor-oss/coding-agent-test"*
or *"review my local changes in ~/src/app"*, or *"how many of my runs failed?"*. Read-only questions are answered directly; **starting,
terminating, or retrying a run pops a confirmation** first. After it starts a run, press
`ctrl+o` (or `/open`) to jump into the live Run Detail view.

Slash commands: `/dashboard` · `/sessions` (browse & resume past chats) · `/templates`
(manage prompt templates) · `/open [id]` · `/folder [id]` (open a run's working folder in your
editor) · `/register` (update task/workflow definitions and run the worker gate) ·
`/new` · `/help` · `/quit`.

**What changed.** A finished run's result card lists the **changed files with status**
(`A` created · `M` updated · `D` deleted; older runs show `•`), aggregated across all
parallel forks — and selecting a completed fork in the task tree shows that fork's own
files. Press **`f`** to open the file picker and `enter` on a file to open *that file* in
your editor. In chat, `get_run` includes the list, so "what files did it create?" just works.

**Open in your editor.** When a run's code is on this host, press `e` in Run Detail or on
the Dashboard (or `/folder` in chat) to open its working folder in your editor/IDE —
`$VISUAL`/`$EDITOR`, else `code`/`cursor`/`subl`/`zed`, else the OS "open" (override with
`--editor` or `$CONDUCTOR_HARNESS_EDITOR`). This is the review bridge: the TUI hands off to
your real tools — it does not embed an editor, diff, or file browser. It's a same-host
convenience (the working dir must be on the machine running the TUI); most useful for
**code_parallel** (which has no PR) and **design_docs** (read/edit the generated markdown).
GitHub-flow runs still review via the PR (`o`).

**Design review loop.** Before chat starts `code_parallel` (directly or through a GitHub
workflow), it asks whether you want design docs and waits for an explicit yes/no. If enabled,
`design_docs` writes the docs and pauses at a human gate after each pass: **Approve** continues to
coding; **Request changes** requires feedback and revises the docs in the next pass. Turn off
human review to use a structured, read-only coding-agent judge. Design iterations default to 5 and can be raised in
Advanced settings. The standalone `design_docs` launcher uses the same loop.

**Feature campaign checkpoints.** Launch `feature_campaign` for complex local work. Its Run
Detail view shows the current phase/wave, integration and check results, active profiles,
review findings, sessions, and file changes. The checkpoint modal supports Continue, editable
Revise feedback, Adopt edits already made in the worktree, Run checks (profile plus optional
check IDs), Set profiles, Stop, and Later. Attached-server checks expose a fresh confirmation
switch; the harness never tears an attached server down.

**Local checkout workspaces.** Every launcher has a **Keep worktree** option. GitHub launchers
also accept an optional **Local checkout**. Chat understands requests such as “address PR 17
using `~/src/app`”. The TUI expands the path and the workflow creates a resumable
`.cc-worktrees/run-<workflow-id>` workspace; the source folder's current branch and dirty edits
are left alone.

**Review local changes.** Choose **Review local changes** (or ask chat to review a local folder)
to run `local_review` before committing. This is intentionally not a worktree workflow: it reads
the checkout you supply, fetches the chosen remote baseline (`origin/main` by default), and
reviews local commits, staged/unstaged edits, and untracked files. It never edits, stages,
commits, pushes, or posts findings to GitHub; its result card shows the structured verdict and
findings.

**OpenSpec development.** Launch `openspec_development` with a target repository, spec source,
and change ID. The form accepts local, Git, and public HTTPS archive sources, shows the selected
child workflow and lifecycle result in Run Detail, and exposes any external archive draft PR on
the result card. Auto mode chooses parallel work only for a safe single wave; complex changes
open the existing feature-campaign checkpoints.

**Prompt templates.** A *Prompt template* fully overrides an agent step's prompt. When you launch
a workflow, a **Prompt template** picker at the top of the form lists
the templates that apply to it: **exactly one → it's auto-selected** (no clicks); **several → you
pick** (or leave Built-in); **none → just Built-in**. The chosen template's text drops into the
**Advanced ▸ Prompt template (edit)** box where you can tweak it (or **Load default** to start
from the shipped built-in, **Save as…** to store the current text). Whatever's in that box is what
runs. The file body is copied into the durable workflow input, and the library path is stored in
the paired `*PromptTemplateSource` field. Run results show the worker's actual resolved source and
content hash, so `input:inline`, `repo:.conductor/<key>.md`, and `bundled:<key>` are unambiguous.

Every TUI launch path uses the same resolver: forms, chat `start_workflow`, and schedule creation
all consult the local library for **every prompt role**, not just the primary picker. A uniquely
applicable template is copied into the workflow input before confirmation/start. Multiple equally
specific templates for the same role block the launch instead of choosing one silently. Roles with
no local match stay blank so the worker then consults the target repo's `.conductor/<key>.md` and
finally the bundled default.

**Scoping controls what shows in the picker.** When you save/create a template you can scope it to
a **workflow** and to a **list of repos** (`owner/name`, comma-separated; blank = any repo). The
launcher filters the picker by the workflow *and* the repo you're targeting — a template scoped to
`conductor-oss/python-sdk` only appears (and can auto-select) when you review a PR on that repo,
and the list re-filters as you type the repo. The library lives in `~/.conductor-harness/templates/`
(override with `$CONDUCTOR_HARNESS_HOME`). Manage it from the **Templates** screen (`t` on the
Dashboard, or `/templates` in chat): **enter/e** opens a template in your external editor, **n**
creates one (name → workflow scope → optional repos; pre-filled with the built-in default for that
workflow), **d** deletes. The built-in defaults are the canonical files in
`workers/defaults/prompts/` — the same text the worker uses by default. For automation the same
override can live in the *target repo* as `.conductor/<key>.md` (`pr_review`/`code`/`plan`/
`design`/`address_pr`), applied with no input. In chat, just describe the guidance ("review for
security") and the agent passes it as the `*PromptTemplate` input. See
`../docs/CODING_AGENT_WORKER.md` §14.

Templates saved from a workflow field include a role mapping such as
`fields: [planPromptTemplate]` in their frontmatter. Existing files without `fields` remain
compatible and apply to that workflow's primary role (`fixPromptTemplate` for `address_pr`,
`reviewPromptTemplate` for `pr_review`, `codePromptTemplate` for coding workflows). Add `fields`
manually when maintaining multiple role-specific templates for one workflow.

The Automations editor stores resolved templates in the schedule's workflow input. Its primary
editor accepts arbitrary inline text or `@repo/path`; unique local-library templates for other
roles are also attached, and editing preserves additional non-secret template inputs supplied
through the API/CLI.

**Approval Inbox and publication gates.** `pr_review`, `address_pr`, and `issue_to_pr` can hold
their completed artifact before a GitHub side effect. Open the global inbox with **`a`** from any
screen; it discovers signal-based WAIT tasks in parent and nested executions, excludes timed
WAITs, and shows the repository, phase, artifact, checks, age, and owning execution. The
app-wide five-second poller keeps the top-bar count and native notifications current.

Actions are **Approve**, **Revise with feedback**, **Stop**, and **Later**; review and PR text is
editable before approval. For `pr_review` and `address_pr`, Revise starts a new producer
execution in the same worktree and returns to a human gate, while Stop records suppression and
publishes nothing. In `approvalMode:"llm"`, those workflows first use the bounded
`maxApprovalRevisions` budget (default `2`) for automatic judge-feedback revisions, then enter
this same inbox if rejection continues. `issue_to_pr` currently escalates an LLM rejection
directly to the inbox. Legacy callers remain compatible: omitted `approvalMode` preserves
`approve`/`approvePr`, and legacy `address_pr` remains ungated.

**Notifications**: on a run's completion the TUI rings the terminal bell and posts a
desktop notification. For a **clickable** notification, install `terminal-notifier`
(`brew install terminal-notifier`). Approval alerts open the Approval Inbox inside the running
TUI and jump directly to the decision dialog when exactly one actionable approval is pending;
other alerts focus the terminal that owns the TUI. Without `terminal-notifier`,
macOS falls back to a plain, non-clickable `osascript` notification. Common terminals are
detected through `TERM_PROGRAM`; set `CONDUCTOR_TUI_BUNDLE_ID` to the terminal application's
bundle ID when using an unrecognized terminal.

**Sessions** persist to `~/.conductor-harness/sessions/` (override with
`$CONDUCTOR_HARNESS_HOME`) — one JSON per conversation, including the runs it launched.
Each launch starts fresh; `/sessions` or `--resume last|<id>` continues one (the agent
replays context and remembers what it started). Without `ANTHROPIC_API_KEY`, chat shows a
guidance panel and you can still `/dashboard`.

## Screens

- **Dashboard** — the fleet: the 50 most recent runs (status · workflow · target ·
  duration · tokens · cost), a worker-health strip (the #1 hang footgun, at a glance),
  filter (`f`: ALL/RUNNING/FAILED) and search (`/`).
- **New run** (`n`) — pick an action (Review local changes / Review a PR / Resolve an issue / Address feedback /
  Run a feature campaign / Code a change / Generate design docs), fill a form (defaults prefilled, Advanced
  collapsed, `gh` issue/PR pickers when available), preflight-checked, and launch.
- **Run detail** (`enter`) — the task tree (recurses into `code_parallel`'s parallel
  forks, including campaign waves, each with its own live turn count) beside a live agent trace (the per-turn
  commands/tokens the workers publish). On completion it becomes a result card with the
  PR/review URL one keypress away. Terminate / retry / logs from here.

## Keys

Chat: type to talk · slash commands above · `ctrl+o` open last run.
Dashboard: `n` new · `enter` open · `f` filter · `/` search · `o` browser · `e` open folder ·
`t` templates · `g` register definitions · `q` quit. Run Detail: `a` review gate (approve/edit/reject when paused) · `f` changed files
(enter opens one in editor) · `e` open folder · `o` open PR/URL · `t` terminate · `r` retry ·
`l` logs · `y` copy id · `c` Conductor UI · `esc` back.

## Layout

```
__main__.py   entry (python -m tui)          catalog.py  workflow specs (forms/targets/cards)
config.py     settings (server, notify, model) api.py     async Conductor client + models
format.py     tokens/cost/duration/glyphs    gh.py       optional issue/PR pickers
notify.py     bell + OS notification         app.py      the Textual App
chat/         llm (tool-use loop) · tools · prompt · session (persistence)
screens/      chat · sessions · dashboard · launcher · run_detail
widgets/      run health · task_tree · turn_log · result_card · preflight · modals
tests/        catalog drift · api parsing · chat (session/tools/llm) · screen pilots
```

## Test

```bash
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest tests/ -q
```

The catalog test asserts the launcher forms never drift from the registered workflow
JSONs; api/screen tests run against captured fixtures with no live server.

## Scope

Chat-driven + form-driven: kick off · monitor · **respond to publication and campaign gates** ·
terminate/retry · list/search, with session persistence + resume. **Not** yet: injecting a
message into an actively executing agent/tool call, mid-session model switching, session
branching, per-session cost caps, browser UI, multi-server profiles. `github_demo` is hidden
from the form launcher (a plumbing smoke test) but its runs still appear on the Dashboard, and
chat can start it if asked.
