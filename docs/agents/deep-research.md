# Deep Research

!!! note "🚧 Coming soon"
    This harness is on the roadmap and not yet published. Want to help build it — or add your own? See [Add your agent](../contributing.md).

A multi-source research agent that **fans out searches, reads, synthesizes, and cites** — running each source in parallel and verifying claims before it writes.

## What it will do

- Decompose a question and fan out searches across many sources at once.
- Read and rank candidates, then synthesize a cited answer grounded in the sources.
- Adversarially verify claims before they land in the final report.

## Conductor features

| Feature | Role |
|---|---|
| `FORK_JOIN_DYNAMIC` | Parallel search + read across an unknown number of sources, joined automatically |
| Synthesis step | Rerank and compose a cited answer from the gathered evidence |
| Durable execution | Long multi-source runs survive restarts without losing gathered work |

[← Back to catalog](../index.md){ .md-button }
