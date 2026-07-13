# Harness TUI — Implementation Guide

Hand-off spec for building the harness TUI (v1). Pairs with **[`TUI_UX.md`](TUI_UX.md)** —
that doc is the *what/why* (screens, journeys, decisions); this is the *how* (stack, APIs,
modules, data shapes, tests). Build to `TUI_UX.md`'s v1 scope; HITL is explicitly v2.

The TUI is a **read-mostly client of the Conductor REST API** — the same API the workers and
the `conductor` CLI use. It starts / terminates / retries workflows and renders their live
state. It changes **no** worker, workflow, or server code. All coding guardrails stay in the
harness; the TUI never touches git/GitHub itself except optional local `gh` for pickers.

---

## 1. Stack & project layout

- **Python 3.12+**, [Textual](https://textual.textualize.io) `>=0.60` (async TUI, CSS-styled
  widgets, mouse+keyboard, works over SSH), [httpx](https://www.python-httpx.org) `>=0.27`
  (async HTTP). No other runtime deps. Keep it isolated from the workers' venv.
- New top-level package **`tui/`** with its own `requirements.txt`. Entry: `python -m tui`.

```
tui/
  __init__.py
  __main__.py        # arg parse (--server, --no-notify), build App, App().run()
  config.py          # Settings: server_url (env CONDUCTOR_SERVER_URL or --server), notify flag
  api.py             # ConductorClient (httpx.AsyncClient) — all REST calls + typed models
  catalog.py         # WORKFLOW CATALOG — the single source of truth (forms, targets, cards)
  gh.py              # optional gh pickers (issue/PR lists); shutil.which-guarded
  notify.py          # terminal bell + OS notification (osascript / notify-send), best-effort
  format.py          # humanize helpers: tokens (54.8k/1.2M), cost ($0.05), durations, glyphs
  app.py             # ConductorHarnessApp(App): screen install, global bindings, poll clock
  screens/
    dashboard.py
    launcher.py
    run_detail.py
  widgets/
    run_table.py     # DataTable of runs
    worker_health.py # header health strip
    task_tree.py     # Tree of tasks, recurses into sub-workflows
    turn_log.py      # live agent turn/command trace
    result_card.py   # terminal-state result panel (per-workflow)
    preflight.py     # server/def/workers checks in the launcher
    logs_modal.py    # task logs overlay
  theme.tcss         # Textual CSS (one dark theme)
  tests/
    test_catalog.py
    test_api.py
    test_screens.py
    fixtures/        # canned execution JSON captured from real runs
  requirements.txt   # textual>=0.60, httpx>=0.27
  README.md          # install + run
```

Design rule: **all workflow-specific knowledge lives in `catalog.py`.** Adding a future
workflow = one catalog entry; screens/widgets stay generic.

---

## 2. Conductor REST API (verified against OSS 3.x on this server)

Base URL = `CONDUCTOR_SERVER_URL` (default `http://localhost:8080/api`). All JSON unless noted.
`ConductorClient` wraps one `httpx.AsyncClient(base_url=...)`, ~10s timeout, and raises a typed
`ConductorError` on non-2xx (surfaced as the "server unreachable" screen for connect errors).

| Purpose | Call | Notes |
|---|---|---|
| **List/search runs** | `GET /workflow/search?start=0&size=50&sort=startTime:DESC&freeText=*&query=<q>` | `q` = `workflowType IN (pr_review,issue_to_pr,address_pr,code_parallel,github_demo)` (URL-encode). Returns `{results:[{workflowId,workflowType,status,startTime,endTime,input,...}], totalHits}`. `startTime`/`endTime` are ISO-8601 strings here. |
| **Full execution** | `GET /workflow/{id}?includeTasks=true` | Returns `{status, workflowId, workflowName, startTime, endTime(ms|absent), input, output, reasonForIncompletion?, tasks:[…]}`. Each task: `{referenceTaskName, taskDefName, taskType, status, taskId, outputData, reasonForIncompletion?, subWorkflowId?}`. `startTime`/`endTime` on the execution here are epoch **ms**; don't assume — parse both int-ms and ISO defensively. |
| **Sub-workflow recursion** | (same endpoint on `task.subWorkflowId`) | A `SUB_WORKFLOW` task carries `subWorkflowId` (confirmed) → fetch that execution for its tasks. Max real depth = 2 (issue_to_pr → code_parallel → code_subtask). |
| **Task logs** | `GET /tasks/{taskId}/log` | `[{log, taskId, createdTime}]`. |
| **Worker liveness** | `GET /tasks/queue/polldata?taskType=coding_agent` (and `=commit` for gitops) | `[{workerId, lastPollTime(ms), queueSize}]`. Fresh if `now-lastPollTime < 15s`. Empty array = nobody polling. |
| **Start a run** | `POST /workflow/{name}` body = input JSON | Returns the new `workflowId` as **plain text** (not JSON) — read `resp.text.strip()`. |
| **Terminate** | `DELETE /workflow/{id}?reason=<text>` | 200 on success, 404 unknown. Cascades to sub-workflows. |
| **Retry** | `POST /workflow/{id}/retry?resumeSubworkflowTasks=false` | Retries from the last failed task. |

**Do NOT reach for the `conductor` CLI** from the TUI — call the REST API directly (the CLI is
for humans/agents; the TUI is a first-class client).

---

## 3. The workflow catalog (`catalog.py`)

The heart of the app. One entry per **launchable** workflow (v1: `pr_review`, `issue_to_pr`,
`address_pr`, `code_parallel` — **not** `github_demo`, hidden per UX decision 4). Each entry
drives the launcher form, the dashboard "target" column, and the result card.

```python
@dataclass(frozen=True)
class Field:
    name: str                     # workflow input key
    label: str
    kind: str                     # "text" | "int" | "float" | "bool" | "enum" | "gh_issue" | "gh_pr"
    default: object = None        # None => required
    required: bool = False
    help: str = ""
    choices: tuple[str, ...] = () # for enum
    advanced: bool = False        # collapsed under "Advanced"

@dataclass(frozen=True)
class WorkflowSpec:
    name: str                     # conductor workflow name
    action: str                   # human label, e.g. "Review a pull request"
    blurb: str
    fields: tuple[Field, ...]
    target: Callable[[dict], str] # input dict -> dashboard target string
    result: Callable[[dict], list[tuple[str,str]]]  # output dict -> card rows (label,value); may include URLs

CATALOG: dict[str, WorkflowSpec] = { ... }
LAUNCHABLE = ["pr_review", "issue_to_pr", "address_pr", "code_parallel"]  # launcher order
```

**Field specs must match the registered defaults exactly** (verified — a test enforces this,
§7). Reference values from the workflow JSONs:

- **`pr_review`** — `repo`* (text), `prNumber`* (gh_pr), `agent` enum[claude,codex,gemini]=claude,
  `model` (text, adv, ""), `maxTurns` int=20 (adv), `maxBudgetUsd` float=1.5 (adv), `timeoutS` int=600 (adv).
- **`issue_to_pr`** — `repo`* (text), `issueNumber`* (gh_issue), `base` text=main,
  `planAgent`/`codeAgent`/`designAgent` enum=claude, `design` bool=false, `maxSubtasks` int=4,
  `maxTurns` int=30 (adv), `maxBudgetUsd` float=2.0 (adv), `timeoutS` int=600 (adv).
  *(Expose a single `Backend` selector that sets `planAgent`+`codeAgent` together; put the
  individual agents under Advanced.)*
- **`address_pr`** — `repo`* (text), `prNumber`* (gh_pr), `engine` enum[code_parallel,coding_agent]=code_parallel,
  `agent` enum=claude, `maxSubtasks` int=4 (adv), `maxTurns` int=20 (adv), `maxBudgetUsd` float=2.0 (adv),
  `timeoutS` int=600 (adv).
- **`code_parallel`** — `repoPath`* (text — a local dir), `instruction`* (text, multiline),
  `changeBranch` text=code-parallel, `design` bool=false, `maxSubtasks` int=6,
  `planAgent`/`codeAgent` enum=claude (backend selector as above), `designAgent` enum=claude (adv),
  `planModel`/`codeModel`/`designModel` (adv, ""), `maxTurns` int=40 (adv), `maxBudgetUsd` float=2.0 (adv),
  `timeoutS` int=600 (adv).

Omit any input left at its default from the start payload (send only what the user set/changed)
— the workflows apply their own `inputTemplate` defaults server-side.

**`target(input)`** examples: `pr_review`/`address_pr` → `"{repo}#PR{prNumber}"`; `issue_to_pr`
→ `"{repo}#{issueNumber}"`; `code_parallel` → `"{repoPath}"`. Repo is shortened to `owner/name`.

**`result(output)`** maps terminal outputs to card rows (from §outputParameters, verified):
- `pr_review` → review URL (`reviewUrl`), verdict (`event`), inline count (`inlineCount`),
  changed files count, tokens (`tokenUsed`)/cost (`costUsd`). Primary link: `reviewUrl`.
- `issue_to_pr` → PR (`prNumber`,`prUrl`), branch (`changeBranch`), subtasks (len), conflicts,
  `totalTokens`/`totalCostUsd`. Primary link: `prUrl`.
- `address_pr` → pushed (`pushed`), reply (`replyUrl`), engine, `totalTokens` (or `agentResult`
  summary when engine=coding_agent). Primary link: `replyUrl`.
- `code_parallel` → branch (`changeBranch`), merged/conflicts, subtasks, `totalTokens`/`totalCostUsd`.
  No URL (local). Show the branch.

The primary link is what `o` opens (`webbrowser.open`).

---

## 4. Data model (`api.py`)

Lightweight dataclasses built from the JSON (don't pass raw dicts around the UI):

```python
@dataclass
class Run:                      # from search results OR execution
    id: str; workflow: str; status: str
    start_ms: int; end_ms: int | None
    input: dict; output: dict
    # derived: target (via catalog), duration, tokens, cost

@dataclass
class TaskNode:
    ref: str; def_name: str; type: str; status: str
    task_id: str; output: dict
    sub_workflow_id: str | None
    children: list["TaskNode"]  # populated by recursion for SUB_WORKFLOW/FORK
    # helpers: is_coding_agent, snapshot() -> AgentSnapshot | None

@dataclass
class AgentSnapshot:            # from a coding_agent task's outputData (live or final)
    status: str                 # IN_PROGRESS | success | error_* | timeout
    num_turns: int; tokens: int; cost: float
    turns: list[dict]           # each: {turn, commands[], tools[], text, tokens}
    running: bool; elapsed_s: float | None
    agent: str; model: str; denials: list[str]
```

**`coding_agent` output shape (verified, live + final):**
`{status, agent, model, filesChanged, result, structured, sessionId, turns[], numTurns,
tokenUsed, costUsd, denials}` plus, on interim IN_PROGRESS pushes, `running`, `elapsedSeconds`.
Each `turns[]` item: `{turn, commands[], tools[], text, tokens}`. This is the ProgressReporter
payload — the TUI is its first real consumer; render `turns[].commands` as the live trace.

**Aggregate tokens/cost while RUNNING**: sum `tokenUsed`/`costUsd` across all coding_agent
`TaskNode`s in the (recursed) tree, including in-progress snapshots — mirrors the recursive
accounting the workflows report on completion. Use the final `output.totalTokens`/`totalCostUsd`
once terminal.

`ConductorClient` methods: `search_runs(limit=50)`, `get_run(id, recurse=True, only_running=True)`
(recurse expands `subWorkflowId`; `only_running` limits recursion fan-out to running/expanded
branches — see polling), `start(name, input)->id`, `terminate(id, reason)`, `retry(id)`,
`task_logs(task_id)`, `health()->{coding_agent:PollState, gitops:PollState}`.

---

## 5. Screens (build to `TUI_UX.md` mockups)

### Dashboard (`screens/dashboard.py`)
- `RunTable` (Textual `DataTable`): columns status·workflow·id(8)·target·duration·tokens·cost.
  Rows from `search_runs(50)`, newest first. Color the status cell (`format.status_style`).
- `WorkerHealth` header strip: `health()` every 5s; green `✓ Ns` per module if fresh (<15s),
  red "workers down — runs will hang" banner otherwise.
- Poll: refresh the run list every **5s** (whole-list search is cheap). Selection preserved
  across refreshes by workflowId.
- Bindings: `n` launcher (prefill from selected run's `input`), `enter` run detail, `f` cycle
  status filter (ALL→RUNNING→FAILED), `/` free-text filter (client-side over target/id/workflow),
  `o` `webbrowser.open(f"{base_ui}/execution/{id}")`, `q` quit, `?` help.

### Launcher (`screens/launcher.py`)
- Step 1: `ListView` of `LAUNCHABLE` (action + blurb). Step 2: form built from the spec's
  `fields` — required first, common next, `advanced` collapsed in a `Collapsible`. Widgets by
  `kind`: `Input` (text/int/float w/ validators), `Switch` (bool), `RadioSet`/`Select` (enum),
  gh picker (below) for `gh_issue`/`gh_pr`.
- `Preflight` widget: on mount + when `repo` changes, check server reachable, `GET
  /metadata/workflow/{name}` exists, `health()` fresh. Show ✓/✗ + fix hint; **disable Start
  until all ✓** (or allow override with a warning — server/def failures block, worker-stale warns).
- Start: build payload (only non-default fields) → `client.start(name, payload)` → push
  RunDetail(new_id). Errors surface inline.
- Prefill mode: constructor takes an optional `input` dict → pre-populate fields.

### Run Detail (`screens/run_detail.py`)
- Header: workflow · id · target · status · elapsed · live aggregate tokens/cost.
- `TaskTree` (left, Textual `Tree`): nodes in execution order with status glyphs; `SUB_WORKFLOW`
  nodes expandable → children from recursion; `coding_agent` nodes show live `▂▄▆ N turns`.
- Right pane: if a `coding_agent` node is selected (default: busiest running one), `TurnLog`
  renders its `AgentSnapshot` (streaming `turn N · command` lines, denials red, token/elapsed
  header, auto-follow). If the run is terminal, swap to `ResultCard` (from `spec.result(output)`).
- Poll: **2s** for this execution; recurse only into RUNNING or user-expanded sub-workflows to
  cap request fan-out. Stop polling when terminal (then render the card + fire notify once).
- Bindings: `t` terminate (confirm modal, states consequences from UX §4), `r` retry (only if
  FAILED), `l` `LogsModal` for the selected task, `o` open primary URL, `c` Conductor UI, `y`
  copy id (`App.copy_to_clipboard`), `←/→` or click to select task, `esc` back.

---

## 6. Cross-cutting

- **Polling** (`app.py` owns a set_interval clock, or each screen owns its own timer that stops
  on unmount): Dashboard 5s, RunDetail 2s, health 5s. Never block the event loop — all calls are
  `await`ed on the async client. A slow/failed poll shows a transient "reconnecting…" indicator,
  not a crash.
- **Notifications** (`notify.py`, v1): when a run started/opened **this session** reaches a
  terminal state, `print("\a")` (bell) + OS notification — macOS
  `osascript -e 'display notification "…" with title "…"'`, Linux `notify-send`; both via
  `shutil.which`-guarded subprocess, best-effort (never raise). Suppressed by `--no-notify`.
  Track "this session" ids in an `App`-level set.
- **gh pickers** (`gh.py`): if `shutil.which("gh")` and `repo` is set,
  `gh issue list --repo <slug> --json number,title --limit 30` / `gh pr list …` populate a
  searchable `Select`. Any failure (no gh, not authed, timeout) → silently fall back to a plain
  number `Input`. Never block the form.
- **Errors / empty states** (build all from UX "States & edge UX" table): server unreachable →
  full-screen retry card; workers stale → red banner + launcher block; run stuck >60s → amber
  row hint; task FAILED → red node + reason + logs + retry; too-small terminal → single-column.
- **`format.py`**: `tokens(n)` → `1.2M`/`54.8k`; `cost(f)` → `$0.05`; `duration(start,end|now)`
  → `MM:SS`/`HH:MM:SS`; `status_glyph/style`. Parse times defensively (int-ms or ISO string).
- **Config**: `CONDUCTOR_SERVER_URL` (strip trailing `/api` to derive the web-UI base for `o`/`c`
  links: `{base}/execution/{id}`). `--server` overrides; `--no-notify` flag.

---

## 7. Testing

1. **`test_catalog.py`** (no network): for each `LAUNCHABLE` spec, load the matching
   `workers/workflows/{name}.json`, assert every catalog `Field` with a non-None default equals
   the workflow's `inputTemplate` value, and every `required` field is a real `inputParameter`
   with no template default. This keeps the forms honest as workflows evolve.
2. **`test_api.py`** (no network): feed canned execution JSON (`tests/fixtures/*.json`, captured
   from real runs — e.g. an `issue_to_pr` with a `code_parallel` sub-workflow, and a `pr_review`)
   into the `TaskNode` builder; assert recursion depth, `AgentSnapshot` extraction, and
   aggregate token/cost summing. Mock `httpx` with `httpx.MockTransport` for client method tests.
3. **`test_screens.py`** (Textual pilot, `async with app.run_test() as pilot`): Dashboard renders
   a mocked run list and status filter cycles; Launcher builds the `pr_review` form and blocks
   Start with an empty required field; RunDetail renders a canned RUNNING execution (task tree +
   live turn trace) and a canned COMPLETED one (result card with the review URL).
4. **Manual live checklist** (against the running server, workers up): `python -m tui` shows the
   ~50 existing runs + green health strip; launch a real `pr_review` on the test repo from the
   launcher → watch the task tree + streaming turn trace → result card shows the review URL, `o`
   opens it; launch a `code_parallel` (or `issue_to_pr`) → confirm sub-workflow fork expansion
   with independent live turn counts; terminate a throwaway run with `t` (confirm modal).

---

## 8. Build order (suggested for the implementing agent)

1. `config.py` + `api.py` (client + models) + `catalog.py`, with `test_catalog.py` +
   `test_api.py` green against fixtures. This is the foundation; get it right first.
2. `format.py`, `app.py` shell + `theme.tcss`, Dashboard with `RunTable` + `WorkerHealth`
   (read-only fleet view working against the live server).
3. Run Detail: `TaskTree` (+ recursion) → `TurnLog` → `ResultCard` → `LogsModal`. Verify live
   against an in-flight run.
4. Launcher: action list → form builder → `Preflight` → start. Then `gh.py` pickers.
5. `notify.py`, terminate/retry confirm modals, edge-state screens, `--no-notify`.
6. `tui/README.md`; add a short "TUI" section to the root `README.md` and `workers/README.md`.

No worker/server/workflow changes anywhere. If a workflow gains inputs later, update its
`catalog.py` entry (and the test will flag drift).

## 9. Out of scope (v1)

HITL/attention surface + workflow gates (v2 — see `TUI_UX.md` §HITL), browser UI, Slack/webhook
notifications, multi-server profiles, cost charts, log follow-mode, pause/resume, auth
management. The TUI inherits `CONDUCTOR_SERVER_URL` + local `gh` exactly like the CLI.
