# Prompt templates

Prompt templates add repository conventions, review focus, or task-specific guidance without
changing a workflow definition. They replace the agent’s user prompt; harness guardrails and the
structured output schema remain enforced.

## Resolution order

For each prompt role the worker chooses one source, highest precedence first:

1. An explicit `*PromptTemplate` workflow input. It can be inline text or `@repo/relative/path`.
2. A committed repository file, `.conductor/<templateKey>.md`.
3. The bundled default in `workers/defaults/prompts/`.

The worker records the actual `requestedSource`, `resolvedSource`, template key, and SHA-256 in
`output.promptTemplate`. A paired `*PromptTemplateSource` input is provenance only; it does not
grant file access or override the resolved content.

## Roles and context

Common inputs are `reviewPromptTemplate`, `planPromptTemplate`, `codePromptTemplate`,
`designPromptTemplate`, and `fixPromptTemplate`. Campaign and OpenSpec workflows expose their
phase-specific variants; the [workflow input reference](workflow-inputs.md) is authoritative.

Templates use `{{placeholder}}` values from the workflow context. Typical names are `{{diff}}`,
`{{feedback}}`, `{{instruction}}`, and `{{subtask}}`. Context that is not consumed by a
placeholder is appended to the prompt, so a concise style-only template still receives the task
details.

```bash
conductor workflow start --workflow pr_review -i '{
  "repo":"acme/service",
  "prNumber":42,
  "reviewPromptTemplate":"Review for authorization and secret-handling mistakes.\n\n{{diff}}",
  "reviewPromptTemplateSource":"cli:security-review"
}'
```

## TUI library

The TUI stores templates below `$CONDUCTOR_HARNESS_HOME/templates/` (normally
`~/.conductor-harness/templates/`). Open the manager with `t` on the dashboard or `/templates`.
Templates can be scoped to workflows and repositories. A role-specific file declares its target
in frontmatter:

```markdown
---
name: Service planning rules
workflows: [code_parallel, address_pr]
repos: [acme/service]
fields: [planPromptTemplate]
---
Keep changes backward-compatible and list migration risks.
```

Legacy files without `fields` still target the workflow’s primary prompt role. A unique most-
specific template is copied into the durable launch or schedule input. Multiple equally specific
matches block the launch until the user makes an explicit choice. Forms, chat, schedules, and
run-again use the same resolution rules.

## Trust boundary

Repository templates and `@repo/path` are repository-controlled input. Treat them like source
code. For untrusted repositories set `CODING_AGENT_REPO_TEMPLATES=0`; inline input remains
available. Paths are constrained to the worktree, template keys are basename-sanitized, and keys
or credentials must never be passed in templates or workflow inputs.
