# Local OpenSpec development

Use `openspec_development` when an apply-ready OpenSpec change is the implementation contract.
The workflow validates the change, derives a repository-aware plan, routes simple disjoint work to
`code_parallel` and dependent/risky work to `feature_campaign`, verifies requirements, then
completes and archives the change.

## Source modes

`specSource` can be a local path, Git remote, or public HTTPS `.zip`, `.tar.gz`, or `.tgz` bundle.
Set `specSourceType` to `local`, `git`, or `url` only when auto-detection is ambiguous. The change
must already be apply-ready; this workflow does not author a proposal.

| Mode | Required inputs | Result |
|---|---|---|
| Same target repository | `repoPath`, `specSource`, `changeId` | Runs in an owned worktree and retains the verified branch. |
| Local source checkout | absolute `specSource`, `changeId`, `useSpecSourceWorkspace:true` | Uses that checkout’s repository for the owned worktree, then pushes and opens a draft PR after verification. |
| Git remote | `repoPath`, remote `specSource`, `changeId` | Fetches the source and uses the target repository for implementation. |
| Public archive | `repoPath`, archive `specSource`, `changeId`, `specWritebackRepo` | Validates the archive and writes the completed change to a draft-PR writeback repository. |

## Checked-out local source

Use this mode when the OpenSpec files are already checked out locally—especially when they are
ignored, such as `design/openspec`. `repoPath` is intentionally omitted because the source
checkout determines the implementation repository.

```bash
conductor workflow start --workflow openspec_development -i '{
  "specSource":"/absolute/path/to/repo/design/openspec",
  "changeId":"add-health-endpoint",
  "useSpecSourceWorkspace":true
}'
```

The workflow creates a harness-owned worktree from committed `HEAD`, materializes only the
declared OpenSpec tree there, and never switches or cleans the original checkout. On success it
force-stages only the declared ignored OpenSpec path, commits the implementation/archive, pushes
the branch, and creates a draft PR. A failed validation or verification creates no source-checkout
cleanup and no PR.

## Safety and operations

Public archives are size-limited, reject symlinks/links, and must resolve to public HTTPS.
Credential-bearing URLs and inline secrets are rejected; use authenticated `gh` and worker
environment credentials instead. `keepWorktree` defaults to true for diagnosis. Set
`executionMode` only to override the conservative automatic router, and use
`checksConfig`/`finalProfile` for the final verification profile.

See [workflow inputs](workflow-inputs.md#openspec_development) for the full contract and
[models and profiles](model-profiles.md) or [templates](templates.md) for optional policy and
prompt controls.
