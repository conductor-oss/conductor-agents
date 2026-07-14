# Claude Agent SDK — Reference for Building Autonomous Coding Agents

> Compiled 2026-07-06 from https://code.claude.com/docs/en/agent-sdk/ (overview, agent-loop,
> typescript, python, permissions, hooks, user-input, custom-tools, subagents, sessions,
> structured-outputs, modifying-system-prompts, streaming-vs-single-mode, hosting,
> secure-deployment). Verify against the changelogs before relying on version-gated behavior:
> https://github.com/anthropics/claude-agent-sdk-typescript / claude-agent-sdk-python

The Claude Agent SDK packages Claude Code's full agent loop — tools, permissions, context
management, sessions, subagents — as a library for TypeScript
(`@anthropic-ai/claude-agent-sdk`, Node 18+) and Python (`claude-agent-sdk`, Python 3.10+).
This document covers how it works, how to intercept and control everything it executes, its
gotchas and limitations, and a reference configuration for unattended (autonomous) coding
agents such as conductor-code workers.

---

## 1. Architecture: the subprocess model

The SDK is not a thin API wrapper. Every `query()` call spawns a **`claude` CLI subprocess**
(a native binary bundled with the SDK package — no separate Claude Code install needed) and
talks to it over stdio using a control protocol. That subprocess owns:

- a shell and a working directory (`cwd` option; defaults to your process's cwd),
- JSONL session transcripts on local disk,
- the agent loop itself (model calls, tool execution, compaction).

Consequences that shape everything else:

- **1 session = 1 subprocess.** N concurrent agents = N processes, each with its own memory
  footprint (~1 GiB RAM / 1 CPU / 5 GiB disk is a reasonable floor; memory grows with session
  length).
- **State is local-disk by default** — transcripts under `~/.claude/projects/<encoded-cwd>/`,
  memory files, working-dir artifacts. None of it survives a container restart unless you
  persist it deliberately (see §10).
- **The bundled CLI binary is pinned to the SDK package version.** Updating the SDK is how you
  update the runtime. Several behaviors below are gated on specific Claude Code versions.
- **Network**: outbound HTTPS to `api.anthropic.com` (or Bedrock/Vertex/Foundry endpoints).
  Auth via `ANTHROPIC_API_KEY`; claude.ai login is explicitly *not permitted* for third-party
  products built on the SDK.

### The agent loop

1. Prompt + system prompt + tool definitions + history go to Claude. SDK yields a
   `system:init` message with session metadata.
2. Claude responds with text and/or tool-call requests (`AssistantMessage`).
3. The SDK executes the tools (hooks can intercept — §7) and feeds results back
   (`UserMessage` with tool results). One full cycle = one **turn**.
4. Repeat until Claude produces a response with no tool calls.
5. SDK yields a final `AssistantMessage`, then a `ResultMessage` with text, cost, usage,
   and session ID.

`maxTurns` counts tool-use round trips only. `maxBudgetUsd` caps estimated spend. Read-only
tools (Read/Glob/Grep and MCP tools annotated `readOnlyHint: true`) run in parallel within a
turn; mutating tools (Edit/Write/Bash, and custom tools by default) run sequentially.

---

## 2. Core API

### TypeScript

```typescript
import { query } from "@anthropic-ai/claude-agent-sdk";

const q = query({
  prompt: "Fix the failing tests in auth.ts",   // string | AsyncIterable<SDKUserMessage>
  options: {
    model: "claude-sonnet-5",
    cwd: "/work/task-worktree",
    allowedTools: ["Read", "Edit", "Write", "Bash", "Glob", "Grep"],
    permissionMode: "dontAsk",
    maxTurns: 50,
    maxBudgetUsd: 50.0,
    effort: "high",
  },
});

for await (const message of q) { /* ... */ }
```

`query()` returns a `Query` (extends `AsyncGenerator<SDKMessage>`) with mid-session control
methods — most require streaming input mode: `interrupt()`, `setPermissionMode()`,
`setModel()`, `applyFlagSettings()`, `setMcpServers()` / `reconnectMcpServer()` /
`toggleMcpServer()`, `rewindFiles()` (needs `enableFileCheckpointing: true`),
`streamInput()`, `stopTask()`, `close()`, plus introspection (`supportedModels()`,
`supportedAgents()`, `mcpServerStatus()`, `accountInfo()`, `initializationResult()`).

Other top-level functions: `startup()` (pre-warm a subprocess before the prompt exists —
useful for latency-sensitive services), `tool()` + `createSdkMcpServer()` (§8),
`listSessions()` / `getSessionMessages()` / `getSessionInfo()` / `renameSession()` /
`tagSession()` (§10), `resolveSettings()` (alpha; resolve effective settings without
spawning the CLI).

### Python

Two entry points:

| | `query()` | `ClaudeSDKClient` |
|---|---|---|
| Session | new per call | held across calls |
| Multi-turn | manual (`resume` / `continue_conversation`) | automatic |
| Interrupts | no | yes (`interrupt()`, `set_permission_mode()`, `set_model()`) |
| Use | one-shot tasks | interactive / conversational |

Options object is `ClaudeAgentOptions` (snake_case fields: `allowed_tools`,
`permission_mode`, `max_turns`, `max_budget_usd`, `setting_sources`, `output_format`, ...).
Message types are checked with `isinstance()` (`AssistantMessage`, `ResultMessage`,
`SystemMessage`, blocks `TextBlock` / `ToolUseBlock` / `ThinkingBlock`).

> **Wire-format quirk**: inside Python's `AgentDefinition`, multi-word fields keep camelCase
> (`disallowedTools`, `mcpServers`) to match the wire format — an exception to the SDK's
> snake_case convention.

### Input modes

- **Single message** (plain string prompt): simplest; no images, no interrupts, no queued
  messages, no mid-session control. Right for stateless one-shot workers (lambdas, CI steps).
- **Streaming input** (`AsyncIterable` of user messages — the documented recommendation):
  a long-lived session that accepts queued messages, base64 image attachments, interrupts,
  and dynamic settings changes. Required for Python's `can_use_tool` callback (see §7 gotcha).

Error-surfacing quirks in streaming mode: in TS, an exception thrown inside *your* message
generator surfaces as `Claude Code process aborted by user` (not the original error); in
Python, a generator exception is logged at debug level and the session **stalls silently** —
if a streaming session hangs with no output, check your generator first.

---

## 3. The message stream and result handling

Five core message types drive the loop:

| Type | When | Notes |
|---|---|---|
| `SystemMessage` | lifecycle events | subtypes: `init` (session metadata incl. `session_id`), `compact_boundary` (after compaction), `informational`, `worker_shutting_down`, `mirror_error` (SessionStore write dropped) |
| `AssistantMessage` | after each Claude response | TS wraps the raw API message: content is at `message.message.content`, **not** `message.content` (Python is direct) |
| `UserMessage` | after each tool execution | carries tool results fed back to Claude; `parent_tool_use_id` set for subagent-context messages |
| `StreamEvent` | only with `includePartialMessages` | raw API deltas for live UIs |
| `ResultMessage` | end of loop | the payload a harness cares about |

`ResultMessage` fields: `subtype`, `result` (final text — **only present on `success`**),
`structured_output`, `total_cost_usd`, `usage` (input/output/cache tokens), `num_turns`,
`duration_ms`, `session_id`, `stop_reason` (`end_turn` / `max_tokens` / `refusal`),
`permission_denials`, `errors`.

Result subtypes:

| Subtype | Meaning |
|---|---|
| `success` | finished normally |
| `error_max_turns` | hit `maxTurns` — resume the session with a higher limit |
| `error_max_budget_usd` | hit `maxBudgetUsd` |
| `error_during_execution` | API failure / cancellation interrupted the loop |
| `error_max_structured_output_retries` | structured output never validated (§11) |

**Gotchas:**

- A single-shot `query()` that ends on an error result **yields the result message, then
  raises/throws**. Wrap the iteration in try/catch if the caller must continue; the branches
  handling the error subtype have already run by the time the exception fires. The underlying
  CLI process also exits nonzero. Streaming sessions stay alive instead.
- Trailing system events (e.g. `prompt_suggestion`) can arrive **after** `ResultMessage` —
  iterate the stream to completion rather than `break`ing on the result.
- In Python, `total_cost_usd` and `usage` are optional and can be `None` on error paths —
  guard before formatting.
- To detect model refusals, check `stop_reason === "refusal"`.
- Hooks may not fire when the session ends via `max_turns` — don't put must-run cleanup only
  in a `Stop` hook.

---

## 4. Built-in tools

Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, Monitor (watch a background
process), ToolSearch (on-demand tool loading), Agent (subagents), Skill, AskUserQuestion,
TaskCreate/TaskUpdate, NotebookEdit, and more. Every tool definition costs context on every
request; scope agents to the minimum set via the `tools` option (availability) — see §6 for
the availability-vs-permission distinction.

---

## 5. Permission evaluation order (memorize this)

When Claude requests a tool, the SDK resolves it in a **fixed order**. Understanding this
order is the difference between "my guard runs" and "my guard is silently bypassed":

```
1. Hooks (PreToolUse)          — can deny outright; an allow does NOT skip steps 2-3
2. Deny rules                  — disallowedTools + settings.json deny; win even in bypassPermissions
3. Ask rules                   — settings.json ask; force the call to canUseTool even in bypassPermissions
4. Permission mode             — bypassPermissions approves; acceptEdits approves file ops;
                                 plan routes writes to canUseTool regardless of allow rules
5. Allow rules                 — allowedTools + settings.json allow
6. canUseTool callback         — everything unresolved lands here (skipped in dontAsk → denied)
```

Key rule semantics:

- `allowedTools: ["Read", "Grep"]` — **pre-approves** those tools. Unlisted tools remain
  *available* to Claude; they just fall through to the mode / callback.
- `disallowedTools: ["Bash"]` (bare name) — removes the tool definition from Claude's context
  entirely; Claude never sees it. `disallowedTools: ["Bash(rm *)"]` (scoped) — Bash stays
  visible, matching calls are denied in **every** mode including `bypassPermissions`.
- Scoped allow rules like `Bash(npm *)` approve only matching calls; other Bash calls still
  fall through.
- Tool-name globs: deny rules accept `"*"` and `"mcp__*"`. Allow rules accept globs only
  after a literal server prefix (`mcp__weather__*`); a bare `allowedTools: ["*"]` is ignored
  with a warning.
- Bash commands are parsed into an AST and matched against rules; unparseable commands and
  constructs like `eval` always require approval. This is a permission gate, **not** a
  sandbox — it doesn't reason about what a command does.

### Permission modes

| Mode | Behavior | Autonomous use |
|---|---|---|
| `default` | unmatched tools → `canUseTool`; no callback ⇒ deny | interactive apps |
| `dontAsk` | anything not pre-approved is **denied**; callback never called | **best for headless workers** — explicit tool surface, hard deny |
| `acceptEdits` | auto-approves file edits + fs commands (`mkdir`, `rm`, `mv`, `cp`, `sed`...) within cwd/additionalDirectories | semi-trusted dev-machine automation |
| `plan` | read-only exploration; edits always prompt through `canUseTool` | plan-then-approve pipelines |
| `bypassPermissions` | approves everything reaching step 4; **cannot run as root on Unix** | only inside disposable sandboxes/CI |
| `auto` (TS only) | model classifier approves/denies each call | experimental middle ground |

Mode can be changed mid-session (`setPermissionMode` / `set_permission_mode`) — e.g. start
in `plan`, review the plan, switch to `acceptEdits`.

### Permission gotchas

- **`allowedTools` does NOT constrain `bypassPermissions`.** `allowedTools: ["Read"]` +
  `bypassPermissions` still approves Bash, Write, everything. To block tools in that mode,
  use `disallowedTools`.
- **Anything auto-approved (allow rule, `acceptEdits`, `bypassPermissions`) never reaches
  `canUseTool`.** Security checks placed in the callback are silently bypassed for those
  tools. The TS SDK warns (`CLAUDE_SDK_CAN_USE_TOOL_SHADOWED`, v2.1.198+) when your callback
  is unreachable. The only check that runs on *every* tool call is a `PreToolUse` hook.
- **Subagent inheritance**: when the parent runs `bypassPermissions`, `acceptEdits`, or
  `auto`, subagents inherit that mode and it cannot be overridden per subagent — a subagent
  with a looser system prompt inherits full autonomy. Explicit `ask` rules still force a stop.
- Declarative allow/deny/ask rules in `.claude/settings.json` load only when the `project`
  setting source is enabled (it is by default; if you set `settingSources` explicitly,
  include `"project"`).
- When a tool is denied, Claude receives the denial message as the tool result and usually
  tries another approach — write denial messages as *guidance*, not just "no".

---

## 6. Tool availability vs. permission (two different layers)

| Option | Layer | Effect |
|---|---|---|
| `tools: ["Read", "Grep"]` | availability | only listed built-ins exist in context; MCP tools unaffected |
| `tools: []` | availability | no built-ins at all — Claude can only use your MCP tools |
| `allowedTools` | permission | pre-approval only; does not add/remove tools |
| `disallowedTools` bare name | both | removes from context |
| `disallowedTools` scoped | permission | denies matching calls, tool stays visible |

Prefer removing a tool from availability (Claude never wastes a turn attempting it) over
scoped denial (Claude tries, gets denied, retries differently) when the tool should never
be used. Note: if you pass a `tools` array and still want clarifying questions, you must
include `AskUserQuestion` in it.

---

## 7. Intercepting everything the agent executes

Three interception mechanisms, in order of power:

### 7.1 Hooks — run on EVERY call, in-process, before all other checks

Hooks are async callbacks in *your* process (they consume zero agent context). Registered
per event with optional matchers:

```typescript
options: {
  hooks: {
    PreToolUse: [
      { matcher: "Bash",        hooks: [guardShellCommands] },   // exact name
      { matcher: "Write|Edit",  hooks: [protectSensitivePaths] },// alternatives
      { matcher: "^mcp__",      hooks: [auditMcp] },             // regex (any non-word char ⇒ regex, unanchored)
      { hooks: [logEverything] },                                // no matcher ⇒ all tools
    ],
    PostToolUse: [{ hooks: [auditResults] }],
  },
}
```

Matcher rules: letters/digits/`_-`/space/`,`/`|` only ⇒ exact match with `|`/`,`
alternatives; anything else ⇒ unanchored regex (anchor with `^...$` for whole-string).
`mcp__memory` as an exact string matches **no** tool — use `mcp__memory__.*`. Matchers
filter tool *names* only; filter on file paths inside the callback via
`input.tool_input.file_path`.

Events (Python supports the core set; several are TS-only):

| Event | Fires | TS-only? |
|---|---|---|
| `PreToolUse` | before a tool runs — can allow/deny/ask/defer/modify input | |
| `PostToolUse` | after a tool returns — can append context or **replace output** (`updatedToolOutput`) | |
| `PostToolUseFailure` | tool errored | |
| `UserPromptSubmit` | prompt sent — inject context | |
| `Stop` | agent finished | |
| `SubagentStart` / `SubagentStop` | subagent lifecycle | |
| `PreCompact` | before context compaction — archive the transcript | |
| `PermissionRequest` | a permission dialog would show — notify Slack/pager | |
| `Notification` | status events (`permission_prompt`, `idle_prompt`, ...) | |
| `PostToolBatch`, `MessageDisplay`, `SessionStart`, `SessionEnd`, `Setup`, `TaskCompleted`, `ConfigChange`, `WorktreeCreate/Remove`, `TeammateIdle` | various | TS only |

Hook return shape (same JSON contract as CLI shell hooks):

```typescript
return {
  systemMessage: "shown to the user (needs includeHookEvents to surface)",  // optional
  continue: true,                                    // false stops the agent ("continue_" in Python)
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "allow" | "deny" | "ask" | "defer",
    permissionDecisionReason: "why — Claude reads this on deny",
    updatedInput: { ...modifiedToolInput },          // requires permissionDecision allow|ask
  },
};
// return {} to pass through unchanged
```

Semantics and gotchas:

- Multiple hooks for one event run **in parallel** with non-deterministic completion order;
  most restrictive decision wins: `deny` > `defer` > `ask` > `allow`. Write hooks to be
  independent.
- A hook `allow` does **not** short-circuit deny/ask rules (steps 2–3 still run); a hook
  `deny` blocks even in `bypassPermissions`. This makes `PreToolUse` the correct place for
  non-bypassable guards.
- `updatedInput` must be inside `hookSpecificOutput` and paired with
  `permissionDecision: "allow"` (or `"ask"`); with `"defer"` it's ignored. Return a new
  object, don't mutate `tool_input`.
- `"defer"` ends the query so you can resume it later from the persisted session — the
  escape hatch for human-approval flows where the process can't stay alive.
- Fire-and-forget side effects: return `{ async: true, asyncTimeout: 30000 }`
  (`async_` in Python) — the agent proceeds immediately; the hook can no longer block/modify.
- Catch errors inside hooks: an unhandled exception can interrupt the agent. Hooks default
  to a 60s timeout (`timeout` field on the matcher).
- Hook input carries `session_id`, `cwd`, `hook_event_name`, plus `agent_id`/`agent_type`
  when firing inside a subagent — use these to scope guards to the top-level agent and avoid
  recursive-spawn loops.
- Shell-command hooks from `.claude/settings.json` also run if the corresponding setting
  source is enabled — remember these exist when debugging "who blocked my tool".

### 7.2 `canUseTool` — the runtime approval callback

Fires only for calls **nothing earlier resolved** (see §5). Receives
`(toolName, input, { signal, suggestions, ... })`; returns:

```typescript
{ behavior: "allow", updatedInput: input, updatedPermissions?: PermissionUpdate[] }
{ behavior: "deny", message: "why", interrupt?: true }
```

Capabilities: approve, approve-with-modified-input (Claude is not told you changed it),
approve-and-persist-a-rule (echo back entries from `suggestions`; `localSettings`
destination writes to `.claude/settings.local.json`), deny with guidance, or deny and
redirect via streaming input. It also receives `AskUserQuestion` calls (Claude's structured
clarifying questions: 1–4 questions, 2–4 options each; answer by returning
`updatedInput: { questions, answers }`). `AskUserQuestion` and MCP tools annotated
`_meta["anthropic/requiresUserInteraction"]` reach the callback even when allow rules match
(in `dontAsk` they're denied instead — that mode never prompts).

The callback can stay pending indefinitely; the loop pauses until it returns. For approvals
that outlive your process, use hook `defer` + session resume instead.

> **Python gotcha**: `can_use_tool` requires streaming input mode **and** a dummy
> `PreToolUse` hook returning `{"continue_": True}` to keep the stream open. Without the
> dummy hook the stream closes before the callback is ever invoked.

### 7.3 Declarative rules and settings files

`allowedTools` / `disallowedTools` in code, plus allow/ask/deny rules in
`.claude/settings.json` (project) / `settings.local.json` / `~/.claude/settings.json`
(user) / managed policy settings. Loaded per `settingSources`. TS also accepts an inline
`settings` object on `query()` and `managedSettings` for policy-tier rules from a parent
process.

**Layering strategy for an autonomous agent:** `PreToolUse` hook for non-negotiable guards
(path escapes, `git push --force`, secrets in commands) → bare-name `disallowedTools` to
remove tools entirely → scoped deny rules for dangerous patterns → `allowedTools` for the
expected surface → `dontAsk` so anything unexpected dies loudly instead of hanging.

---

## 8. Custom tools and MCP

### In-process SDK MCP servers

```typescript
import { tool, createSdkMcpServer } from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";

const reportResult = tool(
  "report_result",
  "Report the task outcome to the orchestrator",
  { status: z.enum(["done", "blocked"]), detail: z.string() },
  async (args) => ({ content: [{ type: "text", text: "recorded" }] }),
  { annotations: { readOnlyHint: false } },
);

const server = createSdkMcpServer({ name: "harness", version: "1.0.0", tools: [reportResult] });

// options:
mcpServers: { harness: server },
allowedTools: ["mcp__harness__report_result"],   // or "mcp__harness__*"
```

Python: `@tool(name, description, schema)` decorator (schema is a `{name: type}` dict or
full JSON Schema for enums/optionals) + `create_sdk_mcp_server(...)`.

Rules and gotchas:

- Tool names become `mcp__{server_key}__{tool_name}` — the server key is whatever key you
  used in `mcpServers`.
- Handler contract: return `{ content: [...blocks], structuredContent?, isError? }`.
  **An uncaught exception in a handler kills the entire `query()` call**; return
  `isError: true` so Claude sees the failure as data and can adapt.
- Content blocks: `text`, `image` (raw base64 in `data` + required `mimeType`; no URL field
  — fetch and encode yourself), `audio` (saved to disk, Claude gets the path), `resource`
  (URI is a *label*; content rides inline in `text`/`blob`), `resource_link`.
- With `structuredContent` set, text blocks in `content` are **not** forwarded (assumed
  duplicative); images/resources are. Python's in-process `@tool` forwards only `content` +
  `is_error` — to return `structuredContent` from Python you need a standalone MCP server.
- Custom tools run sequentially unless annotated `readOnlyHint: true`.
- External MCP servers (stdio command / HTTP) plug into the same `mcpServers` map. Each
  server's tool schemas cost context unless tool search defers them (deferred by default on
  first-party API; loaded upfront on Vertex or non-first-party `ANTHROPIC_BASE_URL`).

---

## 9. Subagents and orchestration

Define programmatically via `agents` (recommended for SDK apps; overrides same-named
`.claude/agents/*.md` files):

```typescript
agents: {
  "code-reviewer": {
    description: "Expert reviewer. Use for quality/security review.",  // Claude matches on this
    prompt: "You are a code review specialist...",
    tools: ["Read", "Grep", "Glob"],       // omit ⇒ inherits all
    disallowedTools: ["Agent"],            // prevent nested spawning
    model: "sonnet",                       // per-agent model override
    effort: "high",
    maxTurns: 25,
    background: false,
    permissionMode: "dontAsk",             // ignored if parent mode is bypass/acceptEdits/auto
  },
},
allowedTools: [..., "Agent"],              // Agent tool invocations need approval too
```

Behavior:

- Subagents run in **fresh contexts**: they get their own system prompt, the Agent tool's
  prompt string, CLAUDE.md (via settingSources), and tool definitions — *not* the parent's
  conversation, tool results, or system prompt. Everything the subagent needs must be in the
  prompt string. Only its final message returns to the parent.
- Invocation is by Claude's judgment (matched on `description`) or explicitly by name in the
  prompt ("Use the code-reviewer agent to ...").
- As of Claude Code v2.1.198 subagents run **in the background by default**; Claude sets
  `run_in_background: false` when it needs the result to continue. Force with `background: true`.
- Nesting allowed to depth 5 (v2.1.172+). Messages from subagent contexts carry
  `parent_tool_use_id`; the tool-use block name is `"Agent"` (renamed from `"Task"` in
  v2.1.63 — match both for compatibility).
- **Resumable**: the Agent tool result includes `agentId: <id>`; resume the same session
  (`resume: sessionId`, same `agents` definitions) and prompt "Resume agent <id> ...".
  Subagent transcripts persist independently and survive parent compaction.
- API errors mid-subagent (v2.1.199+): partial text output is returned with a cutoff note;
  a subagent with no text output fails with `Agent terminated early due to an API error`.
- `AskUserQuestion` is **not available inside subagents**.
- Windows: very long subagent prompts can fail on the 8191-char command-line limit.
- Large parallel fanouts hit API rate limits — batch instead of one wide dispatch. For
  dozens-to-hundreds of coordinated agents, the TS SDK (≥0.3.149) exposes a `Workflow` tool
  that moves orchestration into a script outside the conversation.

---

## 10. Sessions, state, and context

### Session mechanics

- Transcripts: `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` (or under
  `$CLAUDE_CONFIG_DIR/projects/`). `<encoded-cwd>` = absolute cwd with every
  non-alphanumeric char replaced by `-`.
- **#1 resume bug**: `resume` from a different `cwd` looks in the wrong directory and
  silently starts a fresh session. cwd must match. Directly relevant to worktree-per-task
  designs — the session is keyed to the worktree path.
- `continue: true` — resume the most recent session in the cwd (no ID tracking).
  `resume: sessionId` — specific session. `forkSession: true` + `resume` — branch history
  into a new session ID; original untouched. Forking branches the *conversation*, not the
  filesystem.
- Capture `session_id` from the `init` system message (TS: direct field; Python: inside
  `SystemMessage.data`) or from `ResultMessage.session_id` (present on success *and* error —
  so you can resume after `error_max_turns` with a higher limit).
- TS-only: `persistSession: false` keeps a session memory-only (Python always writes disk).
- Cross-host resume: ship the JSONL to the same path + cwd on the new host, or configure a
  `SessionStore` adapter (S3/Redis/Postgres reference impls) — note the store is a **mirror**
  (local disk stays authoritative), covers transcripts only (not CLAUDE.md/artifacts), and
  drops a batch after 3 failed attempts with a `mirror_error` system message. Often more
  robust: persist your own summary/diff state and start fresh sessions.
- Sessions persist the *conversation*, not files. To snapshot/revert the agent's file
  changes, enable `enableFileCheckpointing: true` and use `rewindFiles(userMessageId)`.

### Context window management

Everything accumulates: system prompt, tool definitions, CLAUDE.md, history, tool outputs.
Static prefixes are prompt-cached automatically. When the window nears its limit the SDK
**auto-compacts** (summarizes older history) and emits a `compact_boundary` message.

- Compaction can drop instructions from early in the conversation. Persistent rules belong
  in CLAUDE.md (re-injected every request), not the initial prompt. You can steer the
  compactor with a "summary instructions" section in CLAUDE.md, archive full transcripts in
  a `PreCompact` hook, or trigger manually by sending `/compact` as a prompt.
- Keep context lean: subagents for exploratory subtasks (parent grows only by the summary),
  minimal tool sets, low `effort` for mechanical tasks, watch MCP schema loading.

### Filesystem configuration loading (`settingSources`)

Default `query()` options load **all** sources — `user` (`~/.claude/settings.json`),
`project` (`.claude/settings.json`), `local` (`.claude/settings.local.json`) — pulling in
CLAUDE.md, skills, hooks, permission rules, output styles from the machine. For reproducible
autonomous workers **pin this explicitly** (e.g. `settingSources: ["project"]` or `[]`).
Two things load regardless of settingSources: auto-memory
(`~/.claude/projects/<project>/memory/` — disable with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`)
and the global `~/.claude.json` (redirect with `CLAUDE_CONFIG_DIR`).

> **TS env gotcha**: `options.env` *replaces* the subprocess environment — spread
> `...process.env` to keep `PATH` and `ANTHROPIC_API_KEY`. Python's `env` merges on top.

---

## 11. Structured outputs

Pass a JSON Schema; the agent uses any tools it needs, and the SDK validates the final
output against the schema, re-prompting on mismatch:

```typescript
options: {
  outputFormat: { type: "json_schema", schema: z.toJSONSchema(PlanSchema) },  // Python: output_format=...
}
// on success: message.structured_output matches the schema
// on exhausted retries: subtype === "error_max_structured_output_retries"
```

This is the right way for a harness to get machine-readable results from planner/review/
integration agents — no parsing of free text. Supported: basic types, `enum`, `const`,
`required`, nesting, `$ref`. Keep schemas focused (deep nesting + many required fields fail
more), make fields optional when the info may not exist. A model fallback mid-stream can
also retract a completed output, producing the same error subtype — check
`ResultMessage.errors` to distinguish.

---

## 12. System prompts

**The SDK default is NOT the Claude Code prompt.** Unlike `claude -p`, bare SDK usage gets a
minimal tool-calling prompt with none of Claude Code's coding guidelines or safety
instructions. Four choices:

| Configuration | Result |
|---|---|
| (nothing) | minimal prompt — thin tool-calling loop |
| `systemPrompt: { type: "preset", preset: "claude_code" }` | full Claude Code CLI prompt |
| preset + `append: "..."` | CLI prompt + your rules (lowest-risk customization) |
| custom string | only what you write — you own tool guidance + safety instructions |

For a coding agent, use the preset (+ `append` for harness-specific rules). Also:

- `excludeDynamicSections: true` (TS v0.2.98+ / Py v0.1.58+, preset form only) moves
  per-session context (cwd, platform, git flag, OS) into the first user message so a fleet
  of agents running from different directories **shares one prompt-cache entry** — a real
  cost/latency win for parallel workers.
- CLAUDE.md is injected into the *conversation*, not the system prompt, and is controlled by
  `settingSources`, not by the preset.
- Output styles (`.claude/output-styles/*.md`) can replace or extend the preset's coding
  instructions; TS selects via inline `settings: { outputStyle: ... }`; Python has no
  programmatic selector (use `append` instead).

---

## 13. Cost, limits, and observability

- Per-session accounting on every `ResultMessage`: `total_cost_usd`, `usage`
  (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`), `num_turns`, per-model breakdown in `model_usage`.
- Circuit breakers: `maxTurns`, `maxBudgetUsd`, `effort` (low/medium/high/xhigh/max —
  xhigh recommended for coding on Fable 5 / Opus 4.7+ / Sonnet 5), per-subagent `maxTurns`
  and `effort`.
- **There is no top-level wall-clock session timeout** and no per-subagent deadline —
  enforce deadlines yourself (kill the query via `AbortController` / `close()`, or the
  `CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS` stall watchdog for background subagents only).
- OpenTelemetry: set `CLAUDE_CODE_ENABLE_TELEMETRY=1` (+
  `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` for traces) and standard `OTEL_*` exporter vars in
  the environment; spans/metrics/logs export per `query()`. Prompt text and tool inputs are
  excluded by default (opt-in flags exist).
- Token cost dominates infra cost by ~an order of magnitude; budget accordingly.

---

## 14. Deploying autonomous agents securely

Threat model: prompt injection — the agent's behavior is influenced by content it processes
(repo files, web pages, tool outputs). A malicious README can steer an ungated agent.
Defense in depth:

1. **Isolation boundary** (the agent runs *inside* it):
   - `sandbox-runtime` (`@anthropic-ai/sandbox-runtime`): OS-level fs/network restriction
     (bubblewrap/sandbox-exec), built-in domain-allowlist proxy, lowest setup cost. Shares
     the host kernel; no TLS inspection (domain fronting possible).
   - Hardened Docker: `--cap-drop ALL`, `--network none` + Unix-socket proxy, `--read-only`
     + tmpfs, non-root user, memory/pids limits, code mounted `:ro`.
   - gVisor (userspace kernel; multi-tenant grade) or Firecracker microVMs (hardware
     isolation, <125ms boot) for stronger boundaries.
2. **Network egress**: route through a proxy that enforces domain allowlists, injects
   credentials, and logs. `ANTHROPIC_BASE_URL` for API traffic; `HTTP(S)_PROXY` for the rest
   (Node `fetch()` ignores these by default — `NODE_USE_ENV_PROXY=1` on Node 24+; use
   proxychains/iptables for full coverage).
3. **Credentials**: the agent should never hold them. Proxy-inject API keys outside the
   boundary; for git/DB/internal APIs, expose an MCP tool that forwards to an authenticated
   service outside the sandbox. Never mount `~/.ssh`, `~/.aws`, `.env`, `.git-credentials`,
   `*.pem` into the agent's filesystem — even read-only "analyze this repo" mounts leak
   secrets.
4. **Multi-tenant isolation** (shared container): `settingSources: []`,
   `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, per-tenant `CLAUDE_CONFIG_DIR` and `cwd`, per-tenant
   egress rules. Default settings loading will otherwise leak one tenant's CLAUDE.md into
   another's session.
5. **Reserve `bypassPermissions` for disposable environments**; it refuses to run as root on
   Unix. Prefer `dontAsk` + explicit allowlist even inside sandboxes — a denied tool is a
   signal, a bypassed one is silent.

Session lifecycle patterns for hosting: **ephemeral** (container per task, exits when done —
fits CI/workflow workers), **long-running** (persistent containers holding many sessions;
pin sessions to containers by consistent-hashing `sessionId`), **hybrid** (ephemeral
containers hydrating from a SessionStore — the store is mandatory, not optional, in this
pattern). If you don't need infra control at all, Managed Agents is the hosted alternative.

---

## 15. Consolidated gotcha list

Permissions & interception
1. `allowedTools` does not constrain `bypassPermissions` — use `disallowedTools` to block.
2. Auto-approved tools never reach `canUseTool`; only `PreToolUse` hooks see every call.
3. Hook `allow` doesn't skip deny/ask rules; hook `deny` beats everything, everywhere.
4. Hooks for one event run in parallel, most-restrictive-wins, order non-deterministic.
5. Subagents inherit `bypassPermissions`/`acceptEdits`/`auto` from the parent, no override.
6. Python `can_use_tool` needs streaming mode + a dummy `PreToolUse` hook to work at all.
7. `updatedInput` goes inside `hookSpecificOutput` and needs `permissionDecision: "allow"`.
8. Bash AST permission matching is a gate, not a sandbox; `eval` etc. always prompt.

Sessions & state
9. `resume` with mismatched `cwd` silently creates a fresh session.
10. Session state is local disk; containers lose it without a SessionStore or volume.
11. SessionStore mirrors transcripts only, drops batches after 3 failures (`mirror_error`).
12. Fork branches conversation, not files; file changes from a fork are real and shared.
13. Compaction can drop early-conversation instructions — persistent rules go in CLAUDE.md.

Defaults that surprise
14. SDK default system prompt ≠ Claude Code's — set the `claude_code` preset for parity.
15. Default `settingSources` loads user+project+local settings, hooks, CLAUDE.md from the
    host machine — pin explicitly for reproducible workers.
16. Auto-memory and `~/.claude.json` load regardless of `settingSources`.
17. TS `options.env` replaces the environment (spread `process.env`); Python merges.
18. TS message content is at `message.message.content`; Python at `message.content`.

Error handling
19. Single-shot `query()` raises/throws *after* yielding an error result — by design.
20. Messages can arrive after `ResultMessage`; drain the stream, don't `break`.
21. Uncaught exceptions in custom tool handlers kill the whole query — return `isError: true`.
22. TS streaming: generator errors surface as "process aborted by user"; Python: silent stall.
23. Hooks may not fire on `max_turns` termination.
24. Python `total_cost_usd`/`usage` can be `None` on error paths.

Misc
25. MCP matcher `mcp__server` (exact) matches nothing; use `mcp__server__.*`.
26. Tool-use block is named `"Agent"` (was `"Task"` pre-v2.1.63); older names linger in
    `system:init` tools list and `permission_denials`.
27. Python in-process tools can't return `structuredContent`.
28. Windows: 8191-char command-line limit breaks very long subagent prompts.
29. `AskUserQuestion` unavailable in subagents; requires inclusion in `tools` if you set one.
30. Built-in Explore/Plan agents are one-shot (no `agentId`, not resumable).

## 16. Hard limitations

- **No built-in wall-clock timeout** at session or subagent level. In this harness, runtime
  deadlines are configured only on the Conductor task definition; the worker does not add one.
- Memory grows over long sessions; recycle subprocesses periodically.
- One subprocess per session bounds per-host concurrency by RAM.
- Wide parallel subagent fanouts hit API rate limits.
- Single-message input mode: no images, no interrupts, no queued messages.
- Claude.ai (subscription) auth cannot be offered in third-party SDK products.
- Python SDK trails TypeScript: fewer hook events, no `auto` permission mode, no
  `persistSession: false`, no programmatic output-style selection, no Workflow tool.
- The V2 TS session API (`createSession`/send/stream) was removed in 0.3.142 — use `query()`.

---

## 17. Reference configuration: an unattended coding worker

The shape conductor-code workers would use if migrated from `claude` CLI invocation to the
SDK (TypeScript):

```typescript
import { query } from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";

const TaskResult = z.object({
  status: z.enum(["completed", "blocked", "needs_review"]),
  summary: z.string(),
  files_changed: z.array(z.string()),
  test_command: z.string().optional(),
  blockers: z.array(z.string()),
});

async function runCodingTask(worktree: string, taskPrompt: string) {
  let sessionId: string | undefined;

  const q = query({
    prompt: taskPrompt,
    options: {
      cwd: worktree,                                  // sessions are keyed to this path
      model: "claude-sonnet-5",
      effort: "xhigh",

      // Deterministic behavior: CLI-equivalent prompt, cache-friendly, no host leakage
      systemPrompt: {
        type: "preset", preset: "claude_code",
        append: "You are a conductor-code worker. Commit nothing; the harness handles git.",
        excludeDynamicSections: true,                 // fleet-wide prompt cache sharing
      },
      settingSources: ["project"],                    // repo CLAUDE.md + settings only
      env: { ...process.env, CLAUDE_CODE_DISABLE_AUTO_MEMORY: "1" },

      // Explicit tool surface; anything else is denied, not prompted
      permissionMode: "dontAsk",
      allowedTools: ["Read", "Edit", "Write", "Glob", "Grep",
                     "Bash(npm *)", "Bash(npx *)", "Bash(git status*)", "Bash(git diff*)"],
      disallowedTools: ["WebSearch", "WebFetch",      // no network from the agent
                        "Bash(git push*)", "Bash(git commit*)"],

      // Non-bypassable guard on every call
      hooks: {
        PreToolUse: [{ hooks: [async (input) => {
          const t = input as any;
          const path = t.tool_input?.file_path as string | undefined;
          if (path && !path.startsWith(worktree)) {
            return { hookSpecificOutput: {
              hookEventName: "PreToolUse",
              permissionDecision: "deny",
              permissionDecisionReason: `Write outside worktree ${worktree} is not allowed`,
            }};
          }
          return {};
        }] }],
      },

      // Agent limits; the Conductor task definition owns runtime deadlines
      maxTurns: 50,
      maxBudgetUsd: 50.0,

      // Machine-readable result — no output parsing
      outputFormat: { type: "json_schema", schema: z.toJSONSchema(TaskResult) },
    },
  });

  try {
    for await (const message of q) {
      if (message.type === "system" && message.subtype === "init") {
        sessionId = message.session_id;
      }
      if (message.type === "result") {
        const cost = message.total_cost_usd;
        if (message.subtype === "success" && message.structured_output) {
          return { ok: true, result: TaskResult.parse(message.structured_output), cost, sessionId };
        }
        // error_max_turns / error_max_budget_usd: resume `sessionId` from the SAME cwd
        // with raised limits instead of restarting from scratch.
        return { ok: false, reason: message.subtype, cost, sessionId };
      }
    }
  } catch (err) {
    // single-shot query() throws after yielding an error result — result already handled above
    return { ok: false, reason: `process_error: ${err}`, sessionId };
  }
  return { ok: false, reason: "stream_ended_without_result", sessionId };
}
```

Design notes for the harness:

- **Retry = resume.** On `error_max_turns`/`error_max_budget_usd`, resume `sessionId` from
  the same worktree with higher limits — the agent keeps everything it already learned.
  Because sessions key on cwd, keep the worktree alive across retries or the resume will
  silently start fresh (gotcha #9).
- **Runtime deadline.** Configure it only on the Conductor task definition. The harness does
  not wrap agent sessions in a second wall-clock timeout.
- **Planner/reviewer stages** map naturally to `permissionMode: "plan"` + structured output
  (plan JSON), then a second query in `acceptEdits`/`dontAsk` to execute the approved plan —
  or to subagent definitions inside one session when context sharing matters.
- **Human-in-the-loop escalation** without keeping a process alive: a `PreToolUse` hook
  returning `permissionDecision: "defer"` ends the query; persist the session ID, collect
  the approval out-of-band, resume.
- **Cost accounting** rolls up from `total_cost_usd`/`usage` per task into workflow-level
  metrics for free.
- **Sandboxing**: run each worker inside sandbox-runtime or a hardened container (§14);
  treat the in-process guards (hooks/permissions) as the inner layer, not the boundary.
