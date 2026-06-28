# conductor-agents

**Production-grade agent harnesses, orchestrated by [Conductor](https://conductor-oss.org/).**

Real autonomous agents are long-running, parallel, stateful, and failure-prone — they fan out across
many sub-tasks, loop until done, call LLMs and tools, and must survive restarts without losing work.
That is exactly what a durable workflow engine is for. This repo is a growing collection of
**reference agent harnesses**, each a self-contained, runnable project that shows how to build a serious
agent on Conductor primitives — `FORK_JOIN_DYNAMIC` for parallel fan-out, `DO_WHILE` + `LLM_CHAT_COMPLETE`
for ReAct loops, durable state for multi-hour campaigns, and full observability in the Conductor UI.

Each harness lives in its own directory with a `README.md`, a `run.sh`, its Conductor workflow
definitions, and its worker source — so you can clone, read one, and run it.

## Harnesses

| Harness | What it does | Conductor features | Status |
|---|---|---|---|
| **[security-harness](security-harness/)** | Autonomous web-app & API **penetration tester** — crawls, reasons about the attack surface, **actively exploits** (multi-identity, out-of-band-confirmed), triages false positives, and writes a report + SARIF + an attack-graph dossier. Machine-enforceable authorization + capability gating. | `FORK_JOIN_DYNAMIC` (parallel scanners/exploit agents) · `DO_WHILE` + `LLM_CHAT_COMPLETE` (ReAct browser & exploit agents) · iterative-deepening passes · durable multi-hour runs · `GENERATE_PDF` | ✅ **Ready** |
| coding-agent | Autonomous coding agent — plan, edit, run, and verify changes across a repo. | `DO_WHILE` agent loop · sub-workflows per task | 🚧 Coming soon |
| deep-research | Multi-source research agent — fan out searches, read, synthesize, cite. | `FORK_JOIN_DYNAMIC` (parallel research) · synthesis | 🚧 Coming soon |
| customer-support | Tool-using support agent — triage, retrieve, act, escalate. | ReAct loop · tool tasks · human-in-the-loop | 🚧 Coming soon |

## Quickstart

Each harness is self-contained. Clone the repo, `cd` into a harness, and follow its README:

```bash
git clone https://github.com/conductor-oss/conductor-agents
cd conductor-agents/security-harness
cat README.md          # hero + "run in ~30s" quickstart
./run.sh               # auto-boots the stack and runs the bundled local demo
```

## Why Conductor

- **Durable** — a multi-hour agent run survives worker/server restarts; no lost progress.
- **Parallel** — `FORK_JOIN_DYNAMIC` fans work out across every sub-task and joins the results.
- **Agentic** — `DO_WHILE` + `LLM_CHAT_COMPLETE` gives a first-class ReAct loop with native LLM tasks.
- **Observable** — every task, retry, and decision is visible and replayable in the Conductor UI.
- **Composable** — sub-workflows, schedules, and signals compose into larger systems.

Learn more about Conductor at **[conductor-oss.org](https://conductor-oss.org/)**.

## Contributing a harness

A new harness is a top-level directory with: a `README.md` (hero line + a "run in ~30s" block),
a `run.sh` entrypoint, its Conductor workflow JSON, and its worker source. Add a row to the table
above and a matrix entry in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) so it runs under
the shared quality bar (lint + tests).

## License

[Apache-2.0](LICENSE).
