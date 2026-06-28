# Conductor Agents

**A growing catalog of production-grade long-running AI agents, built and running on [Conductor](https://conductor-oss.org/).**

Real autonomous agents are long-running, parallel, stateful, and failure-prone — they fan out across
many sub-tasks, loop until done, call LLMs and tools, and must survive restarts without losing work.
That is exactly what a durable workflow engine is for.

This repo is a **community catalog of reference agent harnesses** — each one a self-contained,
runnable project that shows how to build a serious, production-grade agent on Conductor primitives.
Clone one, read it, run it in minutes.

> ⭐ **If Conductor powers your agents, star the repo →** [github.com/conductor-oss/conductor](https://github.com/conductor-oss/conductor)

---

## Agent Catalog

| Harness | What it does | Conductor features | Status |
|---|---|---|---|
| **[security-harness](security-harness/)** | Autonomous web-app & API **penetration tester** — crawls, reasons about the attack surface, **actively exploits** (multi-identity, out-of-band-confirmed), triages false positives, and writes a report + SARIF + an attack-graph dossier. Machine-enforceable authorization + capability gating. | `FORK_JOIN_DYNAMIC` (parallel scanners/exploit agents) · `DO_WHILE` + `LLM_CHAT_COMPLETE` (ReAct browser & exploit agents) · iterative-deepening passes · durable multi-hour runs · `GENERATE_PDF` | ✅ **Ready** |
| coding-agent | Autonomous coding agent — plan, edit, run, and verify changes across a repo. | `DO_WHILE` agent loop · sub-workflows per task | 🚧 Coming soon |
| deep-research | Multi-source research agent — fan out searches, read, synthesize, cite. | `FORK_JOIN_DYNAMIC` (parallel research) · synthesis | 🚧 Coming soon |
| customer-support | Tool-using support agent — triage, retrieve, act, escalate. | ReAct loop · tool tasks · human-in-the-loop | 🚧 Coming soon |

---

## Quickstart

Each harness is self-contained. Clone the repo, `cd` into a harness, and follow its README:

```bash
git clone https://github.com/conductor-oss/conductor-agents
cd conductor-agents/security-harness
cat README.md          # hero + "run in ~30s" quickstart
./run.sh               # auto-boots the stack and runs the bundled local demo
```

---

## Why Conductor

Conductor was built at Netflix to run mission-critical workflows at scale — it's been open source ever since. When you build an agent on Conductor you get durability, parallelism, and observability that no custom orchestration layer can match:

| Need | Conductor primitive |
|---|---|
| Survive restarts mid-run | Durable execution — state lives in the server, not your process |
| Fan out across 100 sub-tasks | `FORK_JOIN_DYNAMIC` — parallel branches, automatic join |
| ReAct / tool-calling loop | `DO_WHILE` + `LLM_CHAT_COMPLETE` — native LLM tasks |
| See every decision & retry | Full execution history, replayable in the Conductor UI |
| Compose agents into pipelines | Sub-workflows, schedules, human-in-the-loop signals |

**Learn more →** [conductor-oss.org](https://conductor-oss.org) · [Star on GitHub](https://github.com/conductor-oss/conductor) ⭐

---

## Agentspan

Some agents in this catalog use **[Agentspan](https://orkes.io/agentspan)** — an open-source durable runtime for AI agents that compiles agent definitions directly into Conductor workflows. If you prefer a higher-level agent SDK over raw workflow JSON, Agentspan is the place to start.

---

## Add Your Agent to the Catalog

Got a production-grade agent running on Conductor? **We want it here.**

A new harness needs:

- A top-level directory with a `README.md` (hero line + a "run in ~30s" quickstart block)
- A `run.sh` entrypoint that auto-boots the stack
- Conductor workflow JSON definitions
- Worker source in any language

Then:

1. Add a row to the [Agent Catalog](#agent-catalog) table above.
2. Add a matrix entry in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) so it runs under the shared quality bar (lint + tests).
3. Open a PR.

All production-grade, runnable agents are welcome. If it runs on Conductor and solves a real problem, it belongs here.

---

## License

[Apache-2.0](LICENSE).
