# `coding_agent` — Unattended Coding Agent Worker (Design)

> The Conductor worker that runs a locked-down coding session on a selectable
> **backend** — the Claude Agent SDK (default), the OpenAI **Codex SDK**, or the Google
> **Gemini** CLI — behind one uniform result contract. Implements the reference
> configuration in [`CLAUDE_AGENT_SDK.md`](./CLAUDE_AGENT_SDK.md) §17.
>
> Code: `workers/coding_agent/tasks.py` (the task) + `workers/common/coding_agent.py`
> (backend dispatch + Claude driver) + `workers/common/codex.py` (openai-codex SDK
> driver, CLI fallback) + `workers/common/gemini.py` (Gemini CLI driver) +
> `workers/coding_agent/smoke_test.py` (a server-free test). See §12 for backends.

---

## 1. What it is and what it owns

`coding_agent` takes **a worktree + a natural-language task** and runs one autonomous
Claude Agent SDK session that reads, writes, and edits files inside that worktree until
the task is done, then reports what changed, the cost, and a session id for resuming.

It is deliberately **prompt-driven**: there are no separate `language`, `framework`, or
`goal` fields. The full intent — *"write a program in Go that does Y, following these
rules"* — goes into a single `prompt` input. The worker's job is not to model the task;
it is to run the agent **safely and reproducibly** and hand back a structured result.

**It owns:** launching the SDK session, the permission posture, the tool surface, the
worktree write-boundary guard, cost/turn circuit breakers, and result reporting.

**It does NOT own** (by design — those are other workers/steps):

| Concern | Where it lives |
|---|---|
| Creating/removing the worktree | `gitops` / `git.worktree_add` |
| Committing, pushing, merging | `gitops` |
| Running the project's test suite as a gate | `test` / `run_checks` |
| File-scope + secret-scan policy enforcement | `claude_code` worker (see §9) |

The agent runs *inside* the worktree; the harness stages the worktree before and
commits/tests after.

---

## 2. Design: two layers

```
Conductor task "coding_agent"                 workers/coding_agent/tasks.py
  parse inputs → snapshot files → call helper → diff files → structured TaskResult
        │
        ▼
run_coding_agent(prompt, worktree, ...)       workers/common/coding_agent.py
  builds a locked-down ClaudeAgentOptions, drives sdk.query() to completion,
  collects turns / denials / result / cost
        │
        ▼
Claude Agent SDK  →  claude CLI subprocess (owns the shell + the worktree)
```

The split keeps the SDK policy (permission mode, tool allowlist, guard hook, system
prompt) in one reusable place (`common/coding_agent.py`), separate from the Conductor
plumbing (input parsing, git diffing, `TaskResult` shaping) in `tasks.py`. Other
workflows can call the helper directly without Conductor.

Every option maps to a doc §17 recommendation:

| SDK option set by the worker | Purpose (doc ref) |
|---|---|
| `permission_mode="dontAsk"` | unexpected tools are **denied**, never left hanging on a prompt no human will answer (§5) |
| explicit `allowed_tools` + bare-name `disallowed_tools` | fixed tool surface; network + git-mutation tools removed from context (§6) |
| `PreToolUse` worktree-escape hook | the only check that runs on **every** tool call; denies writes outside the worktree (§5, §7.1) |
| `system_prompt` = `claude_code` preset + `append` + `exclude_dynamic_sections` | CLI-equivalent coding behavior; fleet-wide prompt-cache sharing (§12) |
| `setting_sources=["project"]` + `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | reproducible: only repo config loads, no host machine leakage (§10) |
| `max_turns` / `max_budget_usd` + external `timeout_s` | circuit breakers; the SDK has no wall-clock timeout of its own (§13, §16) |
| optional `output_format` | force a validated JSON result instead of prose (§11) |

---

## 3. How you pass the goal, the instructions, and the context

**Everything the agent should know goes into the `prompt` string.** This is the single
channel of intent. Structure it as: *goal → constraints → context → done-criteria.*

Passing a goal like *"write code in language X to do Y"*:

```text
Implement a CLI tool in Go that reads a CSV file path as its first argument and
prints the number of rows (excluding the header) to stdout.

Requirements:
- Single file: main.go, package main.
- Use only the Go standard library (encoding/csv, os, fmt). No external modules.
- Exit non-zero with a message on stderr if the file is missing or unreadable.

Done when: `go run main.go sample.csv` prints the row count. Do not add tests.
```

Guidance for writing effective prompts (mirrors how `claude_code` front-loads context):

- **State the language/framework and file list explicitly** — the worker does not infer
  them. If the task must touch specific files, name them.
- **Inline the authoritative context** (a contract, an API spec, an interface to build
  to) directly in the prompt rather than telling the agent to "go find it" — every
  discovery step is a wasted turn. The caller (or upstream workflow) is responsible for
  assembling that context into the prompt.
- **Give a concrete done-criterion** (a command that should pass, an output that should
  appear). Without one the agent decides when it's finished.
- **Say what NOT to do** for tight tasks ("just write the one file; do not run it") — the
  hello-world smoke test finishes in a single `Write` because the prompt forbids extra
  actions.

There are two kinds of "instructions", at two scopes:

1. **Per-task instructions** → the `prompt`. Task-specific rules, style, acceptance
   criteria, feedback from a prior attempt.
2. **Standing worker instructions** → `WORKER_SYSTEM_APPEND` in `common/coding_agent.py`,
   appended to the `claude_code` system-prompt preset for **every** run:
   > *"You are an unattended conductor-code worker. There is no human to answer prompts.
   > Work only inside the current working directory. Do not commit, push, or run
   > destructive commands — the harness owns git. Finish the task in as few tool calls as
   > possible."*
   Change this to shift behavior across all tasks; it is not a per-task input.

---

## 4. Task input reference

Set on `task.input_data`. Only `prompt` and `worktreePath` are required.

| Input | Type | Default | Meaning |
|---|---|---|---|
| `prompt` | string | — (required) | The full task: goal + constraints + context + done-criteria (§3). |
| `worktreePath` | string | — (required) | Working directory **and** the write boundary the guard hook enforces. Created automatically if it doesn't exist (a symlinked path like macOS `/tmp` is canonicalized to `/private/tmp`). |
| `agent` | `claude` \| `codex` \| `gemini` | inferred | Backend. If omitted, inferred from `model` (gpt-*/o*/codex-* → codex; gemini-* → gemini; else claude). See §12. |
| `model` | string | SDK/CLI default | Model id (e.g. `claude-sonnet-4-6`, or `gpt-5.1` for Codex). Omit for the backend default. |
| `fallbackModel` | string | none | Model used when the primary is overloaded (`529`). **Claude only.** |
| `allowedDomains` | string[] \| CSV | none | Network hosts the OS sandbox may reach (e.g. `registry.npmjs.org`). Default: **no network** from sandboxed Bash. |
| `effort` | string | model default | `low` \| `medium` \| `high` \| `xhigh` \| `max` — reasoning depth vs cost. |
| `maxTurns` | int | `50` | Tool-use round-trip cap before the agent stops (`error_max_turns`). |
| `maxBudgetUsd` | float | `5.0` | Spend cap before the agent stops (`error_max_budget_usd`). |
| `timeoutS` | float | none | **External** wall-clock cap. On expiry the run is abandoned and `sessionId` is returned for resume. The SDK has no timeout of its own. |
| `resumeSessionId` | string | none | Resume a prior session (retry-as-resume, §7). **Must** use the same `worktreePath` it was created in, or it silently starts fresh (unless a shared session store is configured — see §11). |
| `schema` | dict \| JSON string | none | JSON Schema forcing a validated `structured` result instead of prose (§11 output). |
| `settingSources` | list \| CSV \| `"none"` | env or `["project"]` | Which filesystem config loads. `"none"`/`""`/`[]` loads nothing (untrusted-repo lockdown); overrides the `CODING_AGENT_SETTING_SOURCES` env default. |
| `tools` / `allowedTools` / `disallowedTools` | list \| CSV | SDK defaults | Restrict the tool surface. `tools` is an availability gate (can only *remove* built-ins) — e.g. a read-only planner passes `tools: ["Read","Grep","Glob"]`. Tightening only. |
| `includeFileTree` | bool | `true` | Prepend a bounded, `.gitignore`-respecting listing of the working dir to the prompt so the agent skips its reflexive first-turn `ls`/`Glob` (saves a round-trip + tokens). Skipped automatically on resume. Set `false` to disable. |

> The **guardrails themselves** (tool allowlist, denylist, guard hook, setting sources)
> are **not** task inputs — they are fixed operator policy in `common/coding_agent.py`.
> This is intentional: a task author should not be able to loosen the security posture of
> an autonomous agent. See §5 for what is fixed and §10 for how to change it.

---

## 5. Guardrails: the layered model

Guardrails are configured by the **operator** (in code), not the **caller** (per task).
Five layers. Layers 1–4 are the SDK permission flow (deny wins; see
`CLAUDE_AGENT_SDK.md` §5) — they decide *whether a tool call is allowed to run*.
Layer 5 (OS sandbox) is a separate hard boundary underneath Bash — it decides *what a
command that did run can actually touch*, and unlike 1–4 it cannot be talked around by
a clever shell command.

**Layer 0 — Availability trim (`DEFAULT_TOOLS`).**
Only `Read, Write, Edit, Glob, Grep, Bash` exist in the agent's context. `AskUserQuestion`,
`TaskCreate/Update`, `Monitor`, `Agent`, `WebSearch/WebFetch`, `NotebookEdit`, etc. are
removed entirely — the model never sees them, so it can't waste a turn attempting one.

**Layer 1 — Permission mode: `dontAsk`.**
Anything not explicitly pre-approved is denied outright. There is no human to prompt, so
"ask" would mean "hang forever". A denied call is visible in the `denials` output.

**Layer 2 — Tool availability (denylist, `DEFAULT_DISALLOWED_TOOLS`).**
Bare names remove a tool from the agent's context entirely; scoped rules block dangerous
command shapes:
```python
["WebSearch", "WebFetch",                       # no network from the agent
 "Bash(git push*)", "Bash(git commit*)", "Bash(git reset*)",  # harness owns git
 "Bash(rm -rf *)", "Bash(sudo *)"]              # no destructive shell
```

**Layer 3 — Tool permission (allowlist, `DEFAULT_ALLOWED_TOOLS`).**
The pre-approved surface. Everything else falls through to `dontAsk` → denied.
```python
["Read", "Write", "Edit", "Glob", "Grep",
 "Bash(python *)", "Bash(python3 *)", "Bash(node *)", "Bash(npm *)",
 "Bash(npx *)", "Bash(cat *)", "Bash(ls *)", "Bash(pytest *)",
 "Bash(go *)", "Bash(cargo *)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
 # file move/delete/reorg — no built-in tool exists for these
 "Bash(git mv *)", "Bash(git rm *)", "Bash(mv *)", "Bash(rm *)",
 "Bash(mkdir *)", "Bash(cp *)", "Bash(touch *)"]
```
Scoped `Bash(...)` rules approve only matching commands; any other `Bash` invocation is
denied. **Move/delete** verbs are allowed so the agent can reorganize files (Claude Code
has no delete/move tool, and `Write`/`Edit` only create/modify) — bounded by the OS
sandbox (writes confined to the worktree) and the Layer-1 denylist (`rm -rf`/`sudo` stay
blocked; **deny wins over allow**). `mkdir`/`cp`/`touch` are included because the agent
chains them (`mkdir -p sub && mv a sub/`) and Claude Code requires *every* sub-command of
a compound command to be allowed.

**Layer 4 — The worktree-escape guard (`PreToolUse` hook).**
The only check that runs on *every* tool call and cannot be bypassed by any permission
mode. It resolves the real path of any `Write`/`Edit`/`NotebookEdit` target and denies it
if it escapes the worktree root (`..`, absolute paths, symlinks are all resolved first):
```python
if target != root and not target.startswith(root + os.sep):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "...escapes the worktree..."}}
return {}   # in-scope writes fall through to the allowlist
```
The hook never raises — an error inside it logs and allows, so a guard bug can't crash the
agent (it fails open on its own errors, but the allowlist/denylist still apply).

**Layer 5 — OS sandbox on Bash (`sandbox_enabled`, default on).**
Every Bash command runs under the OS sandbox (Seatbelt on macOS, bubblewrap on Linux):
writes confined to the workspace, and **no network** unless `allowedDomains` opens
specific hosts. Both escape valves are shut — `autoAllowBashIfSandboxed: false` (sandboxing
does not auto-approve commands; the allowlist still gates them) and
`allowUnsandboxedCommands: false` (the model cannot opt a command out via
`dangerouslyDisableSandbox`). This closes the two holes the permission layers can't:

- **Interpreter escape.** `Bash(python3 *)` is allowed, and the guard hook only sees
  `Write`/`Edit` — so `python3 -c "open('/tmp/x','w')..."` would otherwise write anywhere.
  Under the sandbox that write fails at the OS level. *(Verified: the escape file is never
  created, while in-worktree `python3 hello.py` still runs.)*
- **Silent network.** `npm install`, `go get`, `pip` reach the network through allowed Bash
  verbs even with `WebFetch` denied. With no `allowedDomains`, the sandbox blocks them; pass
  the exact registries a task needs.

**Reproducibility guardrails (not security, but determinism):**
`setting_sources=["project"]` loads only the repo's `.claude/` config (not the host
machine's user settings), and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` stops auto-memory from
leaking into the system prompt.

> The OS sandbox is macOS/Linux only. For fully untrusted prompts/repos, still run the
> worker inside a container/VM as the outer boundary (`CLAUDE_AGENT_SDK.md` §14) — the
> sandbox hardens Bash, but defense-in-depth wants an outer boundary too.

---

## 6. Output reference

Returned on the `TaskResult` output (COMPLETED on success, FAILED on agent error — but
never raises for an agent that merely didn't finish):

| Field | Type | Meaning |
|---|---|---|
| `status` | string | SDK result subtype: `success`, `error_max_turns`, `error_max_budget_usd`, `error_during_execution`, `error_max_structured_output_retries`, `timeout`. |
| `filesChanged` | string[] | Files created/modified in the worktree (git status diff before→after). |
| `result` | string | The agent's final text (capped), when not using a schema. |
| `structured` | object \| null | Validated JSON when `schema` was passed. |
| `sessionId` | string | Session id for resume (present on success **and** error). |
| `turns` | object[] | Per-turn trace: one entry per assistant turn with `turn` (1-based), `commands` (the tool calls run that turn, e.g. `"$ go run main.go"`, `"Write main.go"`), `tools` (raw tool names), `text` (short prose slice), and `tokens`. |
| `numTurns` | int | Scalar count of tool-use round trips the agent took. |
| `tokenUsed` | int | Total tokens (input + output + cache r/w). |
| `costUsd` | float | Session cost in USD. |
| `denials` | string[] | Tool calls blocked by the guard/permission layers. |
| `retryable` | bool | (on failure) true for `error_max_turns` / `error_max_budget_usd` / `timeout` — resume instead of restart. |
| `stderr` | string | (on failure) tail of the subprocess's stderr, for diagnosing opaque failures. |

---

## 7. Retry-as-resume

When the agent hits a limit (`error_max_turns`, `error_max_budget_usd`, `timeout`), the
task fails with `retryable: true` and a `sessionId`. To continue, re-invoke with the
**same `worktreePath`** and that `sessionId` plus a raised limit — the agent keeps
everything it already read and did, rather than starting from scratch:

```jsonc
// first attempt failed with status=error_max_turns, sessionId=abc123
{ "worktreePath": "/repo/.cc-worktrees/feature",
  "prompt": "Continue and finish the task.",
  "resumeSessionId": "abc123",
  "maxTurns": 100 }
```

Because sessions are keyed to the working directory, the worktree must still exist at the
same path — don't remove it between attempts.

**Multi-host caveat + fix.** Session transcripts are written to the host that ran the
agent (`$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/*.jsonl`). If Conductor retries the
resume on a *different* worker host, that host has no transcript and the resume silently
starts fresh. To make resume work across hosts, set `CODING_AGENT_SESSION_STORE_DIR` to a
**shared path** (NFS / mounted volume) on every worker: the SDK mirrors each transcript
there and hydrates from it on resume. Verified cross-host: an agent resumed under a fresh
`CLAUDE_CONFIG_DIR` still recalled context written in the first run. (`common/session_store.py`
provides `FileSessionStore`; swap in an S3/Redis adapter by implementing the same async
`append`/`load` protocol.) The worktree path must still match across hosts (shared
worktree volume), same as single-host resume.

---

## Progress reporting (live task updates)

A coding session runs for many turns over minutes; Conductor only sees the final
`TaskResult` when the worker function returns, so without help the task looks frozen
at `IN_PROGRESS` the whole time. The worker pushes **interim `IN_PROGRESS` updates** so
users can watch it work:

- **After every turn** — the SDK wrapper's `on_turn` callback fires as each turn
  completes, and the worker pushes the current output.
- **At least every 30 s regardless** — a background heartbeat thread pushes the latest
  snapshot even when a single turn runs longer than that, so the task never looks stuck.

Each interim update sets task status `IN_PROGRESS` with an `output_data` snapshot:

```jsonc
{
  "status": "IN_PROGRESS",
  "running": true,
  "elapsedSeconds": 18.6,
  "numTurns": 10,
  "tokenUsed": 41234,
  "sessionId": "…",
  "turns": [ /* the same per-turn array as the final output, grown so far */ ]
}
```

Watch it with `conductor workflow status <id>` or the UI — `turns` grows and each
entry's `commands` show what the agent just did (`Write step1.py`, `$ python3 step1.py`).
When the function returns, the poller's terminal update overwrites this with the final
`COMPLETED`/`FAILED` result.

Implementation notes (`common/progress.py`, `ProgressReporter`):

- Uses `TaskResourceApi.update_task` with a `TaskResult` built from the running task's
  ids. Push failures are swallowed — **a progress update never breaks the task**.
- **All HTTP pushes run on the reporter's own background thread.** The per-turn
  `update()` only records the snapshot and wakes that thread — no I/O on the caller.
  This matters because the worker is `async def`: every concurrent `coding_agent`
  task shares one event loop (see below), and a blocking push per turn would stall
  them all.
- Pushes are serialized and monotonic: a guard drops any snapshot that would move
  `turns`/`numTurns` backwards. Progress only ever moves forward.
- Automatic — no workflow or task configuration needed. Requires the worker to reach
  the Conductor server (same `CONDUCTOR_SERVER_URL` it polls with).

### Async worker (AsyncTaskRunner)

The `coding_agent` worker is **`async def`**. conductor-python auto-detects coroutine
worker functions (`inspect.iscoroutinefunction`) and runs them on its **AsyncTaskRunner**:
one event loop per worker process, `thread_count=8` becoming an `asyncio.Semaphore(8)` —
so up to 8 coding sessions run concurrently as coroutines, with no thread pool and no
`asyncio.run()` anywhere in the worker path (`run_coding_agent` is itself async and is
awaited directly). The SDK's `sdk.query` stream is awaited natively; the pieces that
genuinely block are pushed off the shared loop with `asyncio.to_thread`:

- the Codex backend (`run_codex_agent` — blocking subprocess streaming, minutes long),
- the file-tree prime (`git ls-files`) and the before/after `git status` calls,
- `ProgressReporter.stop()` (joins the pusher thread).

`common/claude.py`'s `run_agent` stays a **sync** wrapper (`asyncio.run`) — it serves
sync workers only (gitops' `merge_worktrees`), which run on the classic thread-pool
TaskRunner where no loop is running. Never call it from an async worker.

---

## Operator configuration (environment)

Set on the worker process (not per task):

| Env var | Effect |
|---|---|
| `CODING_AGENT_SETTING_SOURCES` | Default `settingSources` for all tasks. Set to `` (empty) or `none` to load **no** repo config (untrusted-repo lockdown); unset ⇒ `["project"]`. Per-task `settingSources` overrides it. |
| `CODING_AGENT_SESSION_STORE_DIR` | Shared dir enabling cross-host resume (above). Unset ⇒ host-local sessions only. |
| `ANTHROPIC_API_KEY` | Required — the SDK subprocess reads it from the environment. |

---

## 8. Examples

**Conductor workflow task:**
```json
{
  "name": "coding_agent",
  "taskReferenceName": "implement_parser",
  "type": "SIMPLE",
  "inputParameters": {
    "worktreePath": "${setup.output.worktreePath}",
    "prompt": "Implement a JSON-to-YAML converter in Python...\n\nRequirements:...\n\nDone when: `python convert.py in.json` writes in.yaml.",
    "model": "claude-sonnet-4-6",
    "effort": "high",
    "maxTurns": 40,
    "maxBudgetUsd": 3.0,
    "timeoutS": 900
  }
}
```

**Forcing a structured result** (e.g. a review/decomposition that must be machine-readable):
```json
{
  "prompt": "Analyze the auth module and list every endpoint.",
  "worktreePath": "/repo/.cc-worktrees/audit",
  "schema": {
    "type": "object",
    "required": ["endpoints"],
    "properties": {
      "endpoints": { "type": "array", "items": {
        "type": "object",
        "properties": { "path": {"type":"string"}, "method": {"type":"string"} }
      }}
    }
  }
}
```

**Direct Python (no Conductor):**
```python
from common.coding_agent import run_coding_agent

res = run_coding_agent(
    "Write hello.py that prints 'Hello, World!'. Just write the one file.",
    worktree="/tmp/wt",
    max_turns=10, max_budget_usd=1.0, timeout_s=180,
)
print(res["status"], res["num_turns"], res["cost_usd"], res["session_id"])
# res also has: ok, result, structured, tokens, denials, turn_log, error
```

The `smoke_test.py` module runs exactly this against a temp worktree and asserts a
one-`Write` result — run it with `.venv/bin/python -m coding_agent.smoke_test`.

---

## 9. Relationship to the `claude_code` worker

Both drive the Claude Agent SDK; they differ in posture and scope enforcement:

| | `coding_agent` (this worker) | `claude_code` (existing) |
|---|---|---|
| Permission mode | `dontAsk` (deny-by-default) | `acceptEdits` (auto-approve edits) |
| Tool surface | fixed allowlist + guard hook | broad; web disabled |
| Scope enforcement | worktree boundary via `PreToolUse` hook (blocks *before* write) | declared-file + secret **policy check after** the run, reverting out-of-scope files |
| System prompt | `claude_code` preset + worker append | default |
| Modes | one: agentic edit | three: `edit` / `generate` / `structured` |
| Best for | untrusted/unattended runs where you want writes blocked at the source | the harness's existing coding groups with post-hoc policy scoring |

`coding_agent` is the "secure by construction" variant; `claude_code` is the
"broad + audited after" variant. They can coexist — pick per workflow.

---

## 10. Extending / customizing guardrails

Guardrails live in `common/coding_agent.py` and `common/tool_policy.py`, not in task inputs.
To change them:

- **Add a language/toolchain** not already present: add its scoped `Bash(...)` rule to
  `DEFAULT_ALLOWED_TOOLS`.
- **Tighten or loosen the standing instructions**: edit `WORKER_SYSTEM_APPEND`.
- **Per-task tool surfaces**: `run_coding_agent` already accepts `allowed_tools` /
  `disallowed_tools` / `setting_sources` overrides — the Conductor task
  (`tasks.py`) does not currently forward them. Expose them there **only if** you want
  callers to *tighten* the surface; forwarding them to let callers *loosen* it would
  defeat the deny-by-default design.
- **A second guard** (e.g. block edits to `package.json`): add another `HookMatcher` to
  the `PreToolUse` list. Multiple hooks run in parallel and the most restrictive decision
  wins.

Known gaps to be aware of:

- The allowlist covers Python/Node/Go/Rust; other toolchains need their `Bash(...)` verb
  added before the agent can run them.
- The OS sandbox is macOS/Linux only. On other platforms (or with `sandbox_enabled=False`)
  the worktree guard hook covers only `Write`/`Edit`/`NotebookEdit`, not writes performed
  inside a `Bash` interpreter — so on those platforms keep the outer container boundary.
- `setting_sources` defaults to `["project"]`, which loads a repo's `.claude/settings.json`
  (its hooks and allow rules) — fine for trusted repos. For untrusted input, lock it down
  with `settingSources: "none"` per task or `CODING_AGENT_SETTING_SOURCES=` on the worker.

---

## 11. Parallelizing the agent (`code_parallel` workflow)

A single `coding_agent` session is sequential (one tool-use turn at a time). To parallelize
*across* independent pieces of work, an **agentic planner** (a read-only `coding_agent` that
reads the repo) decomposes one instruction into independent sub-tasks and Conductor fans them
out — each editing the **same repo** in its own git worktree/branch, merged back at the end.
This is delivered as two workflow definitions plus one small new git task (`prepare_repo`);
everything else reuses existing tasks:

- **`workers/workflows/code_subtask.json`** (sub-workflow, v1) — one parallel unit:
  `worktree_add → coding_agent → commit`. Each fork gets an isolated git worktree/branch,
  the agent edits inside it, and the work is committed so it can be merged.
- **`workers/workflows/code_parallel.json`** (workflow, v1) — the orchestrator:

  ```
  prepare_repo → create_branch → [design_gate: if design → design docs] → plan(coding_agent, read-only)
    → build_forks(JSON_JQ_TRANSFORM) → FORK_JOIN_DYNAMIC ─┬─ code_subtask ─┐
                                                          ├─ code_subtask ─┤→ JOIN → merge_worktrees → aggregate(JQ)
                                                          └─ code_subtask ─┘
  ```

  1. `prepare_repo` — make the repo git-ready: `git init` if needed, set a local identity if
     none is configured, create an initial commit if there's no HEAD. Idempotent; removes the
     "worktree_add needs a committed repo with an identity" footgun.
  2. `create_branch` — cut a change branch so all sub-task branches merge into it, not `main`.
  3. `design_gate` (optional, `design: true`) — generate detailed design docs first (§ Design
     phase below). Committed to the change branch so every fork inherits them.
  4. `plan` (**`coding_agent` in read-only mode**) — the decomposition step is itself an
     agent: it runs with `tools:["Read","Grep","Glob"]` (can't edit) and a `schema`, so it
     **explores the actual repo** (what exists, what's done, what's pending) and returns
     validated `{subtasks:[{id,description,files,testCmd}]}` as `plan.output.structured`. This
     grounds the plan in real repo state and skips work that's already done.
  5. `build_forks` (`JSON_JQ_TRANSFORM`) — reshape `plan.output.structured` into the
     dynamic-fork arrays: a `code_subtask` SUB_WORKFLOW stub per sub-task + an inputs map keyed
     by sub-task id (with a composed `prompt`), plus a `groupIds` CSV.
  6. `FORK_JOIN_DYNAMIC` → `JOIN` — run every `code_subtask` in parallel.
  7. `merge_worktrees` — merge each `cc-group-<id>` branch into the change branch,
     agent-resolving conflicts; reports `merged` / `conflicts`.
  8. `aggregate` (optional JQ) — roll up per-sub-task cost/files/status.

> **Why an agentic planner (not `LLM_CHAT_COMPLETE`)?** Decomposition needs to understand the
> codebase — what's implemented, what's pending, how things are structured. A stateless LLM
> call sees only the prompt; `coding_agent` reads the repo first. It also returns schema-valid
> JSON via the SDK's `output_format` (with automatic retry), sidestepping the `jsonOutput`
> markdown-fence / "must contain the word json" pitfalls of the LLM task. The tradeoff is
> latency/cost — the planner is a bounded agent run (`planMaxTurns`), not an instant call.

### Inputs (`code_parallel`)

| Input | Default (`inputTemplate`) | Meaning |
|---|---|---|
| `repoPath` | — (required) | Target directory. **Need not be a git repo** — `prepare_repo` inits it. |
| `instruction` | — (required) | The overall coding goal to decompose. |
| `changeBranch` | `code-parallel` | Branch the sub-task branches merge into. |
| `planModel` | `""` (backend default) | Model the agentic planner uses; empty = the chosen backend's own default. |
| `planMaxTurns` | `15` | Turn cap for the planner's repo exploration. |
| `codeModel` | `""` (backend default) | Model each `coding_agent` fork uses; empty = the chosen backend's own default. |
| `maxSubtasks` | `6` | Upper bound on the fan-out (guardrail). |
| `maxTurns` / `maxBudgetUsd` / `timeoutS` | `40` / `2.0` / `600` | Per-fork `coding_agent` limits. |

Output: `changeBranch`, `groupIds`, `subtasks`, `merged`, `conflicts`, `mergeCostUsd`,
**`totalTokens`**, **`totalCostUsd`**, and `summary` (per-sub-task status/files/tokens/cost
plus `tokens` / `cost` breakdowns `{plan, design, subtasks, merge}`).

**Token accounting is recursive**: every `coding_agent` task reports `tokenUsed`; each
sub-workflow surfaces its own total in its `outputParameters` (`code_subtask` → its `code`
task's `tokenUsed`; `design_docs` → the design session's `tokenUsed`); `merge_worktrees`
reports the conflict-resolution agent's `tokenUsed`; and the parent's `aggregate` JQ task
sums plan + design + all forks + merge into `totalTokens` / `totalCostUsd`. Every input is
null-guarded (`tonumber? // 0`) so a skipped design gate or a failed fork contributes 0
instead of breaking the total. Known limitation: when a task is retried, only the **last
attempt's** output is visible to task references, so tokens burned by failed earlier
attempts are not included in the totals.

The read-only planner works because the `coding_agent` task now forwards optional
`tools` / `allowedTools` / `disallowedTools` inputs to `run_coding_agent`. `tools` is an
**availability** gate — it can only *remove* built-ins (here, everything except Read/Grep/Glob),
so a caller can tighten the surface (e.g. a planner) but not grant anything the defaults deny.

### Run it

```bash
# Worker must poll BOTH modules (git tasks incl. prepare_repo live in gitops):
CONDUCTOR_SERVER_URL=http://localhost:8080/api WORKER_MODULES=coding_agent,gitops python main.py

conductor task create workers/workflows/taskdefs/prepare_repo.json  # new taskdef
conductor workflow create workers/workflows/code_subtask.json       # register sub-workflow FIRST
conductor workflow create workers/workflows/code_parallel.json      # (the stub pins version:1)

conductor workflow start -w code_parallel -i '{
  "repoPath": "/path/to/repo",
  "instruction": "Implement the pending modules described in the README ...",
  "changeBranch": "cp-demo",
  "maxSubtasks": 3
}'
```

`repoPath` need not be a git repo — `prepare_repo` initializes it. Each fork's live `turns`
progress (§ Progress reporting) is visible per branch, and so is the planner's exploration.

### Requirements & caveats

- **Server LLM key.** The `coding_agent` planner and forks use the Claude Agent SDK, which
  reads `ANTHROPIC_API_KEY` from the **worker's** env (not the Conductor server's). Set it
  where the worker runs.
- **Worker gate.** The worker must poll `gitops` (for `prepare_repo`/`create_branch`/
  `worktree_add`/`commit`/`merge_worktrees`) in addition to `coding_agent`, or those steps hang.
- **Register `code_subtask` before `code_parallel`** — the fork stub pins `version:1`.
- **Git prep + concurrency are handled.** `prepare_repo` guarantees an inited repo with an
  identity and a HEAD commit; `worktree_add`/`commit` serialize on the shared git dir
  (`fcntl.flock` in `common/git.py`) with a git-lock retry, so the simultaneous worktree burst
  from the fork no longer races `.git/index.lock`.
- **Sub-tasks must be independent** (disjoint files). Overlaps surface in
  `merge.output.conflicts` (agent-resolved where possible). This is the **flat-parallel MVP** —
  no dependency ordering; for layered/dependency-aware waves see `cc_program.json` /
  `cc_feature.json`, which run multiple sequential fork/join rounds.
- **Reshape uses `JSON_JQ_TRANSFORM`, not INLINE** — building keyed maps / SUB_WORKFLOW stubs
  is what JQ is for; INLINE/graaljs mis-serializes structured output.

### Design phase (optional)

Set `design: true` to insert a design step between `create_branch` and `plan`: it turns the
requirements into a set of detailed design docs, **commits them to the change branch**, and the
planner + every coding fork read them — so the parallel work shares one coherent design.

It's a `design_gate` SWITCH that runs the **`design_docs` sub-workflow**
(`workers/workflows/design_docs.json`). That workflow is a bounded `DO_WHILE` review loop:

1. `design` (**`coding_agent`**, write mode) — a single agent session writes the whole doc set
   under `docs/design/`: `architecture.md` first (the single source of truth — file layout +
   shared types/interfaces/naming), then supporting docs (data-model, api, ui, testing, …) that
   reuse those contracts. **One session authors them all, so they're mutually consistent** — no
   cross-doc reconciliation needed. Backend-selectable via `designAgent` (Claude or Codex), and
   it reads the existing repo (brownfield-aware).
2. Review the pass. With `designHumanApproval:true` (the default), a `HUMAN` task pauses the
   workflow. Approval exits the loop; rejection completes the gate with actionable feedback and
   the next authoring pass revises the docs. With human review disabled, a read-only
   `coding_agent` judge (`Read`, `Grep`, `Glob` only) emits structured approval + feedback.
3. `commit_design` (`commit`) — runs only after approval and commits `docs/design/` onto the
   change branch. Exhausting the iteration cap fails closed and coding never starts.

Because the docs are committed **before** the fork, each `code_subtask` worktree inherits them
(git `worktree add` branches off HEAD) and the fork's `coding_agent` reads them — the plan and
fork prompts say *"read `docs/design/` (especially `architecture.md`) if present."* No per-fork
plumbing.

| Input | Default | Meaning |
|---|---|---|
| `design` | `false` | Enable the design phase. |
| `designAgent` | `claude` | Backend that authors the docs (`claude` or `codex`). |
| `designModel` | `""` (backend default) | Model for the design agent; empty = the chosen backend's own default. |
| `designDir` | `docs/design` | Committed directory the docs are written to. |
| `designMaxTurns` | `40` | Turn cap for each design-author session. |
| `designHumanApproval` | `true` | Pause for human approval/feedback after every pass. False uses the LLM judge. |
| `designMaxIterations` | `5` | Maximum author/review passes; configurable higher. |
| `designReviewAgent` / `designReviewModel` | `claude` / `""` | Backend/model used by the automated judge. |
| `designReviewMaxTurns` | `5` | Tool-turn cap for each automated judge pass; configurable higher. |

Notes:
- Uses **`coding_agent`**, not `claude_code` — so design is backend-selectable and an all-Codex
  run (`designAgent`/`codeAgent`: `codex`) authors design on Codex too. `code_parallel` no longer
  needs the `claude_code` module; `WORKER_MODULES=coding_agent,gitops` suffices for every path.
- Single-session authorship within each pass plus feedback-driven revision is the consistency
  mechanism (vs. generating docs in isolation, which can diverge on names/types).
- Verified end-to-end: one Claude session wrote 5 consistent docs (architecture + api / data-model
  / testing / ui) where `architecture.md` declares itself the source of truth the others cite; all
  committed ahead of the fork and read by the planner + coders.
- `design_docs` is also runnable standalone (register it before `code_parallel` — the gate pins
  `version:1`).

---

## 12. Backends: Claude Agent SDK (default), OpenAI Codex, or Google Gemini

The worker runs on any of three engines behind one result contract. Pick per task with
the `agent` input; if omitted it's **inferred from the model id** (`gpt-*`/`o*`/`codex-*`
→ codex; `gemini-*` → gemini; `claude-*` or unset → claude). All produce the same output
dict (`status`, `result`, `structured`, `turns`, `sessionId`, `tokenUsed`, `costUsd`, …)
plus an `agent` field recording which ran.

- **Claude** (`common/coding_agent.py`) — the locked-down Agent SDK path (dontAsk + tool
  allowlist + `PreToolUse` worktree-escape hook + `sandbox`), all of §1–§10.
- **Codex** (`common/codex.py`) — the official **`openai-codex` Python SDK** (verified
  against 0.1.0b3): `AsyncCodex` controls the local codex app-server over JSON-RPC
  (the wheel bundles its own runtime; reuses `~/.codex` auth). Native async on the
  worker's event loop, native `output_schema` structured output, `effort`
  pass-through, `thread_resume` sessions, `ApprovalMode.deny_all` (nothing ever blocks
  on an approval — the sandbox governs), and typed per-item notifications for the live
  trace. Timeout = a watchdog that `interrupt()`s the turn server-side (never cancel
  the stream — the SDK's blocking queue reader would leak an executor thread).
  **CLI fallback**: set `CODEX_DRIVER=cli` (or uninstall the SDK) to shell
  `codex exec --json` like before — escape hatch while the SDK is beta.
- **Gemini** (`common/gemini.py`) — drives the open-source **Gemini CLI** headless
  (`gemini -p … -o json`). There is no official Google *agent* SDK for coding (the Gen AI
  SDK is a raw model API — no tools/loop/sandbox), so the CLI is Google's coding-agent
  engine, exactly as Codex's CLI is OpenAI's. Verified against gemini-cli v0.49.0. The
  single JSON output carries `session_id`, `response`, and `stats` (per-model tokens,
  tool-call counts, lines added/removed). Auth: `GEMINI_API_KEY` in the worker env or
  `~/.gemini/.env` (never logged).

**Shared regardless of backend:** worktree creation, the file-tree prime (§4
`includeFileTree`), progress reporting, and retry-as-resume.

### Parity matrix

| Capability | Claude | Codex | Gemini |
|---|---|---|---|
| Edit vs read-only | tool allowlist + `dontAsk` + guard hook | `-s workspace-write` vs `-s read-only` (read-only inferred when `tools`⊆{Read,Grep,Glob}) | `--approval-mode yolo` vs `--approval-mode plan` (same read-only inference) |
| Write boundary | `PreToolUse` hook + `sandbox` | Codex `workspace-write` sandbox | `--sandbox` (Seatbelt on macOS, default profile confines writes to the worktree; docker/podman on Linux). NOTE: default profile leaves **network open** — weaker than Claude's no-network default |
| Structured output | `output_format` (schema) | native `output_schema` param (schema **auto-normalized** to OpenAI strict) | **prompt-enforced** (schema embedded in prompt; fenced/prose-wrapped JSON parsed out; one corrective `--resume` retry on parse failure) — weakest of the three |
| Model | `-m` / `model` | `model` (thread param) | `-m` / `model` |
| Session resume | `resume` session_id | `thread_resume(thread_id)` | `--resume <session_id>` (id from the JSON output) |
| Progress / turns | `AssistantMessage` loop (per turn) | streamed SDK notifications (per item) | **final-only**: one synthesized summary turn from `stats.tools` (json mode has no per-turn stream); heartbeat covers liveness; `numTurns` ≈ tool-call count |
| Cost | native USD | **estimated** from tokens (`cost.py` OpenAI rates) | **estimated** from tokens (`cost.py` Gemini rates) |
| No-ops | — | `fallbackModel`, `settingSources`, `session_store`, `allowedDomains`, fine-grained `allowedTools`/`disallowedTools` (`effort` IS honored by the SDK driver) | `fallbackModel`, `effort`, `settingSources`, `session_store`, `allowedDomains`, fine-grained tool lists |

### Mixing backends in `code_parallel`

`code_parallel` exposes `planAgent` and `codeAgent` (default `claude`), so you can plan
with one engine and code the parallel forks with the other. `build_forks` injects the
chosen `agent` into every fork's `code_subtask` input.

**Cross-backend model guard:** if the `model` id belongs to a different backend than the
selected `agent` (e.g. `codeAgent:"gemini"` while `codeModel` still holds the default
`claude-sonnet-4-6`), the worker drops the model and uses the chosen backend's own
default — logged as a warning — instead of failing on a provider 404. Set the matching
`*Model` explicitly when you want a specific non-default model. Verified end-to-end: `planAgent:
claude` + `codeAgent: codex` planned with Claude and built three modules in parallel on
Codex (each fork `agent=codex`), merged cleanly, all self-tests passing.

```bash
conductor workflow start -w code_parallel -i '{
  "repoPath": "/path/to/repo",
  "instruction": "…",
  "planAgent": "claude",   "planModel": "claude-sonnet-4-6",
  "codeAgent": "codex",    "codeModel": "gpt-5.1"
}'
```

**Prerequisites:** whichever CLIs the run selects must be installed and authenticated on
the worker host — `codex` (`OPENAI_API_KEY` / `codex login`) and/or `gemini`
(`npm i -g @google/gemini-cli`, `GEMINI_API_KEY`). The worker still polls
`WORKER_MODULES=coding_agent,gitops`.

---

## 13. Remote git / GitHub (githops)

The `gitops` module also owns **remote** operations, so the harness can work against real
GitHub repos end to end: clone a repo, push a change branch, and open / inspect / merge
pull requests. Transport (clone/fetch/pull/push/remote) is provider-agnostic `git`; the
`pr_*` tasks are GitHub-specific and shell out to the **`gh` CLI**.

**Auth (one model for both).** `gh` must be authenticated on the worker host
(`gh auth login`, or `GH_TOKEN` / `GITHUB_TOKEN` in the worker env). The first remote task
per process runs `gh auth setup-git`, which registers `gh` as git's credential helper —
so plain `git push/pull/fetch/clone` over HTTPS use gh's token with no URL munging. If gh
isn't authenticated, remote tasks fail with a clear message; local-only flows are
unaffected.

| Task | What it does | Key inputs |
|---|---|---|
| `git_clone` | Clone a remote repo (optionally shallow / a specific branch). | `repoUrl`, `dest?`, `branch?`, `depth?` |
| `git_fetch` | Fetch refs/PRs from a remote. | `repoPath`, `remote?`, `refspec?`, `prune?` |
| `git_pull` | Fetch + integrate (rebase by default). **Fail-soft**: on conflict it aborts and returns `conflicts[]`, leaving the tree clean. | `repoPath`, `remote?`, `branch?`, `rebase?` |
| `git_push` | Push a branch (sets upstream). `--force-with-lease` only when `forceWithLease:true` — never a bare `--force`. | `repoPath`, `branch?`, `remote?`, `setUpstream?`, `forceWithLease?` |
| `git_remote` | Add/set a remote URL (idempotent). | `repoPath`, `url`, `name?` |
| `pr_create` | Open a PR from the change branch. No `title` → `gh --fill` from commits. Returns `{number, url}`. | `repoPath`, `title?`, `body?`, `base?`, `head?`, `draft?`, `fill?` |
| `pr_checkout` | Check out an existing PR by number so the harness can iterate on it. | `repoPath`, `number`, `branch?`, `force?` |
| `pr_status` | Review/merge state + CI checks, with pass/fail/pending counts. | `repoPath`, `number?` |
| `pr_comment` | Post a comment on a PR. Always appends an invisible `<!-- conductor-harness -->` marker so `pr_comments` can skip harness-authored comments. | `repoPath`, `number`, `body` |
| `pr_merge` | Merge a PR (`squash`\|`rebase`\|`merge`; optional `--auto`). **Destructive, opt-in, no retry.** | `repoPath`, `number`, `method?`, `deleteBranch?`, `auto?` |
| `issue_fetch` | Fetch an issue's title/body/labels (seeds a coding instruction). | `repo`, `number` |
| `pr_comments` | Consolidate a PR's feedback (conversation + reviews + inline), skipping harness-authored comments; returns metadata + a single `feedback` blob + `hasFeedback`. | `repo`, `number` |
| `pr_diff` | A PR's unified diff (capped) + changed files, via `gh pr diff` (feeds the read-only reviewer). | `repo`, `number` |
| `pr_submit_review` | Post a formal review — inline file/line comments + summary + verdict — via the reviews REST API. Clamped to COMMENT/REQUEST_CHANGES (**never APPROVE**); falls back to a summary-only review if an inline line doesn't anchor to the diff. | `repo`, `number`, `structured` |

Transport tasks retry (2×, exponential backoff) for transient network blips; `pr_merge`
never retries. Code: `common/git.py` (transport) + `common/github.py` (gh/PR ops) + the
`@worker_task` wrappers in `gitops/tasks.py`.

**Demo workflow `github_demo`** — the full local-change-to-PR loop:
`git_clone → create_branch → coding_agent → commit → git_push → pr_create`.

```bash
conductor workflow start -w github_demo -i '{
  "repoUrl": "https://github.com/you/your-repo.git",
  "instruction": "Add a CONTRIBUTING.md with build and test instructions.",
  "changeBranch": "harness/add-contributing",
  "base": "main"
}'
```

Output includes `prNumber` / `prUrl`. `code_parallel` is unchanged — remote steps are
composed around it (e.g. clone first, push + open a PR after merge) rather than baked in.

**Workflow `issue_to_pr`** — issue → PR: `issue_fetch → git_clone (temp folder) →
code_parallel (sub-workflow) → [approve_gate] → final_pr → git_push → pr_create` (PR body
`Closes #N`). Inputs: `repo`, `issueNumber`, `base`, `approvePr` (default **false**), plus the
usual `planAgent`/`codeAgent`/limits. The **`approvePr` review gate** (see below) sits *before
`git_push` and `pr_create`*, so when it's on nothing reaches the remote until a human approves.

**Workflow `address_pr`** — the PR-feedback loop:
`pr_comments → [feedback_gate: hasFeedback?] → git_clone → pr_checkout → [engine_gate] →
git_push → pr_comment`. Consolidates a PR's review feedback and addresses it on the PR
branch, pushing to the **same branch** (updates the PR — no new PR). The harness's own
replies carry the marker and are skipped, so the loop is safely re-runnable; the outer
gate returns cleanly when there's no outstanding feedback.

The **`engine` input** selects how the coding is done (nested `engine_gate` SWITCH):
- `code_parallel` (default) — embeds the full decompose → parallel forks → merge
  sub-workflow (same core as `issue_to_pr`), reusing the PR branch as its `changeBranch`
  (`pr_checkout` positions HEAD at the PR tip first, so the merged commits land on the PR
  branch). Best for multi-part reviews; it commits internally.
- `coding_agent` — a single session on the PR branch (`+ commit`). Cheapest for small,
  cohesive feedback.

```bash
conductor workflow start -w address_pr -i '{
  "repo": "https://github.com/you/your-repo.git",
  "prNumber": 4,
  "engine": "code_parallel"
}'
```

Scope: `issue_to_pr` / `address_pr` target **same-repo** PRs (matches the testbed); fork-PR
write-back needs the fork flow (future). File move/delete feedback ("move X to a subfolder",
"remove the dead file") is supported — the `coding_agent` tool surface includes the
move/delete verbs (see §6).

**Workflow `pr_review`** — review a PR and post a formal review:
`pr_comments → git_clone → pr_checkout → pr_diff → coding_agent (read-only + review schema)
→ [approve_gate] → final_review → pr_submit_review`. The review *analysis reuses `coding_agent`* — a **read-only** run
(`tools:["Read","Grep","Glob"]`, no write/Bash) with an `output_schema`
(`{summary, verdict, comments:[{path,line,body}]}`), exactly the planner's read-only+structured
pattern. The diff is **pre-computed** (`pr_diff`) and injected so the reviewer stays read-only
(it cannot modify the PR, only comment). `pr_submit_review` posts the findings as inline
file/line comments + a summary; verdict is COMMENT, or REQUEST_CHANGES when the agent flags a
blocking issue — **never APPROVE** (enforced by the schema enum and clamped in the task).

```bash
conductor workflow start -w pr_review -i '{
  "repo": "https://github.com/you/your-repo.git",
  "prNumber": 4
}'
```

`pr_review` + `address_pr` compose the full loop: review a PR → address the feedback.

### Review gate (optional HITL)

Both `pr_review` and `issue_to_pr` carry an **optional human-in-the-loop checkpoint** so a
person can review — and edit — the drafted output before it reaches GitHub. It's built from
Conductor's built-in **`HUMAN`** system task (pauses until signaled; no worker/taskdef), gated
by a **`SWITCH`** on a boolean workflow input that defaults **false** — so
`conductor workflow start` and any automation run gate-off, while the TUI enables it by default.

| Workflow | Input | What the human reviews | Gate position |
|---|---|---|---|
| `pr_review` | `approve` | the drafted review — `summary` · `verdict` · inline `comments` | before `pr_submit_review` (nothing posts) |
| `issue_to_pr` | `approvePr` | the drafted PR — `title` + `body` | before `git_push` + `pr_create` (nothing hits the remote) |

Shape (per workflow): `approve_gate` (SWITCH value-param on the flag) → `"true": [ gate (HUMAN) ]`,
default `[]`. The HUMAN task's `inputParameters.draft` holds what to review (the client reads it
to render/edit). A `JSON_JQ_TRANSFORM` (`final_review` / `final_pr`) then chooses the human's
version when gated (guarding on the flag, tolerant of the unresolved gate ref on the auto path)
and the auto-draft otherwise; the terminal task (`pr_submit_review` / `pr_create`) consumes it.

Signal the gate over REST: `POST /tasks/{workflowId}/{gateRef}/{status}` with the decision as
the JSON body. `COMPLETED` proceeds — body `{approved:true, review:{…}}` for `pr_review`
(the edited structured review), `{approved:true, title, body}` for `issue_to_pr`.
`FAILED_WITH_TERMINAL_ERROR` **rejects** — the terminal task never runs, the workflow ends
FAILED, and nothing is posted/opened. The output flows to `${gateRef.output.*}`.

```bash
# gated review; pause, then approve (optionally edited) from the TUI or:
WID=$(conductor workflow start -w pr_review -i '{"repo":"you/repo","prNumber":4,"approve":true}')
curl -X POST "$CONDUCTOR_SERVER_URL/tasks/$WID/review_gate/COMPLETED" \
  -H 'Content-Type: application/json' \
  -d '{"approved":true,"review":{"summary":"LGTM","verdict":"comment","comments":[]}}'
# …or reject (no review posted, workflow FAILED):
curl -X POST "$CONDUCTOR_SERVER_URL/tasks/$WID/review_gate/FAILED_WITH_TERMINAL_ERROR" \
  -H 'Content-Type: application/json' -d '{"approved":false}'
```

## 14. Prompt templates (user-supplied instructions)

Each workflow ships a well-tuned built-in prompt for its agent step, but you can **fully
override** that prompt — to encode house style, a review focus, domain rules, etc. — from
layered sources, resolved in `coding_agent` (`common/templating.py`, choke point
`coding_agent/tasks.py`). Highest precedence first:

1. **Explicit input** — a per-step `*PromptTemplate` workflow input: either inline text, or
   `@repo/relative/path` to read the prompt from a file in the checkout (e.g.
   `"reviewPromptTemplate": "@.github/review-guide.md"`). Lets you keep the prompt "somewhere"
   of your choosing; a missing/blocked path falls through to the tiers below.
2. **Repo-resident** — a `.conductor/<templateKey>.md` file committed in the *target repo*,
   read from the checked-out worktree. Version-controlled, applies to every run on that repo
   with no payload change (the natural fit for automation/CI).
3. **Shipped default** — `workers/defaults/prompts/<templateKey>.md`, the **canonical built-in
   prompt** the worker uses by default (one file per key, in `{{placeholder}}` form). This is
   the single source of truth for the defaults: the TUI seeds new templates from these exact
   files, and editing a file changes the default the worker runs. (A last-resort inline `prompt`
   in the workflow JSON remains only as a safety net if a default file is ever missing.)

The template becomes the agent's **user prompt** (uniform across Claude/Codex/Gemini — no
system-prompt/backend changes). The `WORKER_SYSTEM_APPEND` guardrail text and the structured
**output `schema` stay harness-owned**, so a fully custom `pr_review` template still yields the
`{summary,verdict,comments}` that `pr_submit_review` needs — the schema is the safety net.

**Rendering.** A chosen template is filled against a `promptContext` map: `{{key}}` placeholders
are substituted, and any *unused* non-empty context entry is appended under a `## Context`
trailer — so a persona-only template still receives the runtime context (diff/feedback/…).
Placeholders with no matching context key are left literal.

**Per-workflow inputs, templateKeys, and context:**

| Workflow | Input | templateKey (repo file) | context placeholders |
|---|---|---|---|
| `pr_review` | `reviewPromptTemplate` | `.conductor/pr_review.md` | `{{diff}}`, `{{feedback}}` |
| `design_docs` | `designPromptTemplate` | `.conductor/design.md` | `{{instruction}}`, `{{designDir}}` |
| `code_parallel` (planner) | `planPromptTemplate` | `.conductor/plan.md` | `{{instruction}}`, `{{maxSubtasks}}` |
| `code_parallel` (subtasks) | `codePromptTemplate` | `.conductor/code.md` | `{{subtask}}` |
| `issue_to_pr` | `planPromptTemplate` / `codePromptTemplate` | (via code_parallel) | as above |
| `address_pr` (single-agent) | `fixPromptTemplate` | `.conductor/address_pr.md` | `{{feedback}}` |
| `address_pr` (code_parallel engine) | `fixPromptTemplate` | `.conductor/code.md` | subtask |

`.conductor/code.md` is the high-value one: a backend-portable, harness-managed coding-guidelines
file applied to every parallel subtask (like CLAUDE.md, but cross-backend and workflow-scoped).
The canonical text for every key lives in `workers/defaults/prompts/` — copy one as the starting
point for your own `.conductor/<key>.md` or `*PromptTemplate`.

```bash
# explicit input (full override of the review prompt):
conductor workflow start -w pr_review -i '{
  "repo": "you/repo", "prNumber": 4,
  "reviewPromptTemplate": "You are a security reviewer. Focus on authz, input validation, and secret handling. Diff:\n{{diff}}"
}'
# or commit .conductor/pr_review.md in the repo and just:
conductor workflow start -w pr_review -i '{"repo":"you/repo","prNumber":4}'
```

**Security / trust.** The repo-resident layer is a repo-controlled injection vector (like
`CLAUDE.md` via `settingSources`) — trust it the same as the code. For untrusted repos disable
it in the worker env: `CODING_AGENT_REPO_TEMPLATES=0` (this also disables `@repo/path` reads,
which pull repo content). An explicit inline `*PromptTemplate` input is unaffected; a
`templateKey` is basename-sanitized so it can't escape `.conductor/`, and `@path` is guarded so
it can't escape the worktree.

## 15. Repository guide (AGENTS.md)

So an automated run needs zero payload to know **how to build, test, and review a repo**, the
worker reads a repo "agent guide" from the checked-out worktree and **prepends it to the prompt
for every `coding_agent` call** — all backends (Claude/Codex/Gemini) and the read-only review /
plan steps alike. Discovery order (first existing, non-empty root file wins):

`AGENTS.md`  →  `AGENT.md`  →  `CLAUDE.md`

`AGENTS.md` is the cross-tool standard; `CLAUDE.md` is kept as a fallback so Codex/Gemini also
benefit. It's injected into the **user prompt** (not a system channel Codex/Gemini lack), cold
start only (a resumed session already saw it), and capped (~24k chars). This is separate from,
and complementary to, the prompt-template layers in §14: the guide is *context* (repo
conventions), the template is the *task prompt*.

- **No double-load for Claude.** Claude already ingests `CLAUDE.md` natively via
  `settingSources: ["project"]`; when the discovered guide is `CLAUDE.md` and that's active, the
  harness skips re-injecting it. `AGENTS.md`/`AGENT.md` are always injected (nothing else reads
  them).
- **Toggle.** On by default. Disable per task with `includeRepoGuide: false`, or fleet-wide with
  the worker env `CODING_AGENT_REPO_GUIDE=0` (same trust rationale as `CODING_AGENT_REPO_TEMPLATES`).

Put build/test/lint commands, architecture notes, and review priorities in `AGENTS.md`; every
harness run on that repo — local or CI — picks them up automatically.
