# Models and profiles

Every harness workflow accepts the same model-policy envelope. Leave it blank to use the resolved
default (a scoped TUI policy when selected, otherwise bundled `standard`), select a named profile
with `modelProfile`, or attach a declarative policy snapshot with `modelPolicy`. The policy selects roles—not just one model—so planning, coding,
review, judging, and summarizing can use different backends.

## Quick start

The TUI creates one editable policy at
`$CONDUCTOR_HARNESS_HOME/model-profiles/models.json` (normally
`~/.conductor-harness/model-profiles/models.json`) on first use. Open **Model Profiles** with
`m` on the dashboard or `/models` in chat, then edit and reload it. The TUI chat model is
separate: changing a workflow profile never changes the conversational model.

For the CLI, select the default bundled profile explicitly:

```bash
conductor workflow start --workflow code_parallel -i '{
  "repoPath":"/absolute/path/to/repo",
  "instruction":"Add health checks and tests",
  "modelProfile":"standard"
}'
```

## Resolution and overrides

The worker resolves policy in this order: bundled defaults, repository
`.conductor-code/models.json`, the TUI/inline snapshot, the selected profile variant, nonblank
legacy agent/model inputs, then `modelOverrides`. Repository policy is read from the prepared
worktree; `modelsConfig` may select another **relative** path inside that worktree.

Use `modelOverrides` for a deliberate role-level exception. Backend/model combinations are
validated and a mismatch fails before agent or repository mutation. `maxTurns` and
`maxBudgetUsd` remain global ceilings: a role tier can only lower them.

```json
{
  "modelProfile": "standard",
  "modelOverrides": {
    "review": { "agent": "codex", "model": "gpt-5.6-terra" }
  }
}
```

The TUI copies the selected user policy and its canonical SHA-256 into launch and schedule
inputs. That makes re-runs durable even if the local policy file later changes. Run details show
the effective role tiers, policy-source hashes, cost labels, and any diversity warning.

## Bundled profiles and roles

Policy version 1 is data-only JSON: it may define profiles, `design`, `plan`, `code`, `review`,
`judge`, and `scribe` roles, tiers, review-loop settings, budgets, and prices. It cannot define
commands, environment variables, or credentials.

| Profile | Intended use | Role behavior |
|---|---|---|
| `legacy` | Compatibility with one-pass Claude-style runs | Claude roles, no automatic profile selection. |
| `trivial` | Small, low-cost changes | Sonnet producers, two review rounds, lower caps. |
| `standard` | Default | Opus design/plan; Sonnet then Opus coding; Codex review/judge; Haiku scribe; eight rounds. |
| `full` | Large or high-confidence work | Standard mapping with twelve rounds and larger aggregate caps. |

An empty Codex model means **Codex CLI default** and is priced as `codex:default`, never as a
Claude model. Provider-reported cost is authoritative. Policy prices supply explicitly marked
estimates when a backend cannot report actual cost. If bundled Codex review/judge is unavailable,
the resolver can fall back to Opus and records a prominent diversity warning; explicit user
overrides fail closed unless they provide their own fallback tier.

## Current provider catalog

The policy files are the executable source of truth. This table is an operator-facing catalog
snapshot for the supplied policies; update the policy and this table together when changing a
default. Confirm availability and contractual pricing with the provider before production use.

| Provider | Model ID | Input / output USD per MTok | Suggested role |
|---|---|---:|---|
| Anthropic | `claude-opus-4-8` | 5 / 25 | Design, planning, escalation |
| Anthropic | `claude-sonnet-5` | 3 / 15 | Default coding tier |
| Anthropic | `claude-haiku-4-5` | 1 / 5 | Scribe and low-cost work |
| OpenAI | `gpt-5.6-sol` | 5 / 30 | Complex reasoning/coding |
| OpenAI | `gpt-5.6-terra` | 2.5 / 15 | Balanced Codex review/coding |
| OpenAI | `gpt-5.6-luna` | 1 / 6 | Cost-sensitive work |

Anthropic’s [models overview](https://platform.claude.com/docs/en/about-claude/models/overview)
and OpenAI’s [model catalog](https://developers.openai.com/api/docs/models) are the authoritative
availability and pricing references. The provider catalogs may include newer models than this
policy snapshot; a new provider model does not change a running harness until an operator edits
policy.

## Policy files

Use [`config/models.example.json`](config/models.example.json) as a valid starter for either a
repository policy or the single TUI user-policy file. Its `workflows` and `repos` metadata scope
automatic TUI selection; explicit `modelProfile` always wins. Equal-specificity user-policy
matches block a launch instead of guessing.

Never put an API key, CLI command, or environment value in a policy. Authenticate Claude, Codex,
and Gemini in the worker environment as described in the [quickstart](quickstart.md).
