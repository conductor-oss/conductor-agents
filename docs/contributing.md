# Add your agent to the catalog

Got a production-grade agent running on Conductor? **We want it here.** The catalog is a collection of self-contained, runnable reference harnesses — clone one, read it, run it in minutes.

## What a new harness needs

- A top-level directory with a `README.md` — a hero line plus a "run in ~30s" quickstart block.
- A concise `SKILL.md` — valid `name` and `description` frontmatter plus operating instructions for AI assistants.
- A `run.sh` entrypoint that auto-boots the stack.
- Conductor workflow JSON definitions.
- Worker source in any language.

## Then

1. Add your agent to the [catalog](index.md) and the root `README.md` table.
2. Add a matrix entry in [`.github/workflows/ci.yml`](https://github.com/conductor-oss/conductor-agents/blob/main/.github/workflows/ci.yml) so it runs under the shared quality bar (lint + tests).
3. Open a PR.

All production-grade, runnable agents are welcome. If it runs on Conductor and solves a real problem, it belongs here.

## Documenting your agent on this site

Each agent's documentation lives **inside its own directory** — this site aggregates it at build time with the [monorepo plugin](https://github.com/backstage/mkdocs-monorepo-plugin). To add your docs:

1. Create a `docs/` folder in your agent directory (e.g. `my-agent/docs/`) with your Markdown pages.
2. Add a `my-agent/mkdocs.yml` declaring the sub-nav:

    ```yaml
    site_name: my-agent      # becomes the URL namespace → /my-agent/…
    nav:
      - Overview: index.md
      - Quickstart: quickstart.md
    ```

3. Reference it from the root `mkdocs.yml` nav:

    ```yaml
    nav:
      - Agents:
          - My Agent: "!include ./my-agent/mkdocs.yml"
    ```

4. Add a card for it on the [catalog homepage](index.md) (`docs/index.md`) so it shows up in the grid.

### Building the site locally

```bash
pip install -r requirements-docs.txt
mkdocs serve          # live-reload at http://127.0.0.1:8000
mkdocs build          # static site → ./site
```
