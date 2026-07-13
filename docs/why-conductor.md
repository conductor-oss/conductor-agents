# Why Conductor

Real autonomous agents are **long-running, parallel, stateful, and failure-prone** — they fan out across many sub-tasks, loop until done, call LLMs and tools, and must survive restarts without losing work. That is exactly what a durable workflow engine is for.

[Conductor](https://conductor-oss.org) was built at Netflix to run mission-critical workflows at scale, and has been open source ever since. When you build an agent on Conductor you get durability, parallelism, and observability that no custom orchestration layer can match.

| Need | Conductor primitive |
|---|---|
| Survive restarts mid-run | Durable execution — state lives in the server, not your process |
| Fan out across 100 sub-tasks | `FORK_JOIN_DYNAMIC` — parallel branches, automatic join |
| ReAct / tool-calling loop | `DO_WHILE` + `LLM_CHAT_COMPLETE` — native LLM tasks |
| See every decision & retry | Full execution history, replayable in the Conductor UI |
| Compose agents into pipelines | Sub-workflows, schedules, human-in-the-loop signals |

Every harness in this catalog runs as a durable Conductor workflow: every step is checkpointed, retried on failure, and replayable. Tool calls, model calls, and human approvals are all first-class, observable steps.

!!! tip "Star Conductor on GitHub"
    If Conductor powers your agents, star the repo → [github.com/conductor-oss/conductor](https://github.com/conductor-oss/conductor) ⭐

## Learn more

<div class="grid cards" markdown>

-   :material-book-open-variant: **[conductor-oss.org](https://conductor-oss.org)**

    Docs, concepts, and getting started.

-   :material-github: **[Star on GitHub](https://github.com/conductor-oss/conductor)**

    The open-source orchestration engine.

-   :material-shield-check: **[Security Harness](../security-harness/)**

    The flagship agent in this catalog — see the primitives in action.

</div>
