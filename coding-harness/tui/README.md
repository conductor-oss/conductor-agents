# Harness TUI

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

Flags: `--model M` (chat driver; default `sonnet`; aliases `sonnet`/`opus`/`haiku` or a
full id like `claude-sonnet-4-6`), `--resume [id]` (continue a past chat; bare = last),
`--dashboard` (land on the dashboard instead of chat), `--server URL`, `--no-notify`.
Requires Python 3.12+, a reachable Conductor server, **`ANTHROPIC_API_KEY`** for chat, and
— for the GitHub workflows — the same `gh` auth the workers use. Min terminal size ~80×24.

## Chat (default screen)

The TUI opens into **chat**: an LLM (the `--model`) interprets what you ask and drives the
harness by calling tools — `list_runs`, `get_run`, `start_workflow`, `terminate_run`,
`retry_run`, plus `gh` issue/PR lookup. Try *"review PR 4 on conductor-oss/coding-agent-test"*
or *"how many of my runs failed?"*. Read-only questions are answered directly; **starting,
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
human review to use a read-only LLM judge. Design iterations and judge tool turns both default to
5 and can be raised in Advanced settings. The standalone `design_docs` launcher uses the same
loop.

**Prompt templates.** A *Prompt template* fully overrides an agent step's prompt (blank =
built-in). When you launch a workflow, a **Prompt template** picker at the top of the form lists
the templates that apply to it: **exactly one → it's auto-selected** (no clicks); **several → you
pick** (or leave Built-in); **none → just Built-in**. The chosen template's text drops into the
**Advanced ▸ Prompt template (edit)** box where you can tweak it (or **Load default** to start
from the shipped built-in, **Save as…** to store the current text). Whatever's in that box is what
runs.

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

**Review before it ships (HITL gate).** `pr_review` and `issue_to_pr` can pause for you to
review — and edit — the drafted output before anything reaches GitHub: `pr_review` holds the
drafted review (summary · verdict · inline comments) before it posts; `issue_to_pr` holds the
drafted PR (title + body) *before push and PR-create*, so nothing hits the remote until you
say so. The gate is **on by default when launched from the TUI** (form checkbox and chat both
default it on) and **off for automation** (`conductor workflow start` / any caller that doesn't
set `approve`/`approvePr`). When a gated run pauses, Run Detail shows a **⏳ needs your review**
banner and auto-opens an approval modal (re-open with **`a`**): **Approve** posts/opens it,
**Reject** ends the run FAILED (nothing posted/opened), **Edit** (`e`) opens the draft as JSON
in your editor — save, then Approve to ship the edited version. Toggle it explicitly with the
"Review before …" checkbox in the launcher, or `approve:false` / `approvePr:false` in chat.

**Notifications**: on a run's completion the TUI rings the terminal bell and posts a
desktop notification. For a **clickable** notification (clicking opens the run in the
browser), install `terminal-notifier` (`brew install terminal-notifier`) — without it,
macOS falls back to a plain, non-clickable `osascript` notification.

**Sessions** persist to `~/.conductor-harness/sessions/` (override with
`$CONDUCTOR_HARNESS_HOME`) — one JSON per conversation, including the runs it launched.
Each launch starts fresh; `/sessions` or `--resume last|<id>` continues one (the agent
replays context and remembers what it started). Without `ANTHROPIC_API_KEY`, chat shows a
guidance panel and you can still `/dashboard`.

## Screens

- **Dashboard** — the fleet: the 50 most recent runs (status · workflow · target ·
  duration · tokens · cost), a worker-health strip (the #1 hang footgun, at a glance),
  filter (`f`: ALL/RUNNING/FAILED) and search (`/`).
- **New run** (`n`) — pick an action (Review a PR / Resolve an issue / Address feedback /
  Code a change / Generate design docs), fill a form (defaults prefilled, Advanced
  collapsed, `gh` issue/PR pickers when available), preflight-checked, and launch.
- **Run detail** (`enter`) — the task tree (recurses into `code_parallel`'s parallel
  forks, each with its own live turn count) beside a live agent trace (the per-turn
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

Chat-driven + form-driven: kick off · monitor · **respond to review gates** (approve / edit /
reject the drafted review or PR before it ships) · terminate/retry · list/search, with session
persistence + resume. **Not** yet: HITL for arbitrary mid-run prompts (only the two
review gates on `pr_review`/`issue_to_pr` are wired), mid-session model switching, session
branching, per-session cost caps, browser UI, multi-server profiles. `github_demo` is hidden
from the form launcher (a plumbing smoke test) but its runs still appear on the Dashboard, and
chat can start it if asked.
