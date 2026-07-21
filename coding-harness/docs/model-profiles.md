# Model profiles

Model policy version 1 is declarative JSON. The worker resolves bundled defaults, a
repository `.conductor-code/models.json`, an optional selected user-policy snapshot,
the selected profile, then nonblank legacy fields and structured `modelOverrides`.
Commands, environment values, and credentials are rejected.

The bundled `standard` profile uses Opus for design/planning, Sonnet then Opus for
coding escalation, Codex CLI default for review/judging, and Haiku for scribing.
`trivial`, `full`, and `legacy` are also available. An empty Codex model is recorded
as `codex:default` for estimated pricing; provider-reported costs remain authoritative.

Pass `modelProfile`, `modelPolicy`, `modelPolicySource`, `modelsConfig`, and/or
`modelOverrides` to a workflow. `modelsConfig` must be a relative path inside the
prepared worktree. The dashboard `m` key opens the Model Profiles browser; user files
live under `$CONDUCTOR_HARNESS_HOME/model-profiles/`.

Revision candidates are saved under workflow-namespaced `refs/conductor/revision/*`.
The checkpoint worker refuses source checkouts and only restores harness-owned worktrees.
