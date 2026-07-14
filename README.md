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

## Harness Catalog

The repository-level README is an index, not an operating guide for any one agent. Each ready
harness owns its setup, safety requirements, workflows, and examples in its directory.

| Harness | Category | Status | Start here |
|---|---|---|---|
| **[security-harness](security-harness/)** | Security testing | ✅ Ready | [README](security-harness/README.md) · [Agent skill](security-harness/SKILL.md) |
| **[coding-harness](coding-harness/)** | Software delivery | ✅ Ready | [README](coding-harness/README.md) · [Agent skill](coding-harness/SKILL.md) |
| deep-research | Research | 🚧 Coming soon | — |
| customer-support | Customer operations | 🚧 Coming soon | — |

---

## Get Started

Each harness is self-contained and may have different prerequisites, safety gates, and entry
points. Choose one from the catalog and follow that harness's README; do not assume commands from
one harness apply to another.

```bash
git clone https://github.com/conductor-oss/conductor-agents
cd conductor-agents/<harness>
cat README.md
```

If an AI assistant is operating the harness, point it at that directory's `SKILL.md`.

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
- A concise `SKILL.md` with `name` and `description` frontmatter plus safe operating instructions
- A `run.sh` entrypoint that auto-boots the stack
- Conductor workflow JSON definitions
- Worker source in any language

Then:

1. Add a row to the [Harness Catalog](#harness-catalog) table above.
2. Add a matrix entry in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) so it runs under the shared quality bar (lint + tests).
3. Open a PR.

All production-grade, runnable agents are welcome. If it runs on Conductor and solves a real problem, it belongs here.

---

## License

[Apache-2.0](LICENSE).
