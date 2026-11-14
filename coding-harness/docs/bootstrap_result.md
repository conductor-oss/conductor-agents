# Greenfield On-Ramp — Phase 5 Result (G3 `project_bootstrap`)

**Status: shipped and live-proven.** A one-paragraph description + an empty directory now become a
buildable, tested skeleton and then the first real feature on top — in one driven sequence. Proven by
a real Conductor execution.

## What shipped

`project_bootstrap` (`workflows/project_bootstrap.json` + `common/bootstrap.py` + the `stages/`
workers):

1. **`bootstrap_init`** git-inits the empty dir with an empty base commit (preserving any existing
   files), so the scaffold can commit on top and verification has a base from run one.
2. A **`coding_agent`** scaffolds a minimal buildable skeleton for the description (stack hints
   honored): the project files, **at least one passing test**, a **Makefile `test` target**, and an
   **AGENTS.md documenting `make test`** so the harness can verify from run one.
3. The **`commit`** worker commits the skeleton.
4. A raw **`test_run`** gates the skeleton **green** under the supplied `verificationCommand`
   (`make test`) — `SKELETON_NOT_GREEN` otherwise, an honest stop.
5. If a `firstFeature` is given, **`code_parallel`** adds it on top (its own workspace, verified).
6. **`bootstrap_outcome`** folds the honest verdict: `BOOTSTRAPPED` / `SKELETON_READY` /
   `FEATURE_INCOMPLETE` / `SKELETON_NOT_GREEN`.

## Live proof — `BOOTSTRAPPED` (`97609b2c-82b8-4be6-b7e9-55cc9d0324b9`)

- **Input:** an empty dir + *"a tiny Python string utility library `stringkit` with `reverse(s)`…"* +
  first feature *"add `shout(s)` returning s uppercased with a trailing `!`."*
- **Trace:** `bootstrap_init` → `coding_agent` scaffold → `commit` → `skeleton_run = passed`
  (`make test` green) → `code_parallel` added the feature (`verified = true`) → `BOOTSTRAPPED`.
- **Independently re-verified** on the branch: the repo contains `AGENTS.md`, `Makefile`,
  `stringkit.py` (both **`reverse`** from the skeleton and **`shout`** from the first feature), and
  `test_stringkit.py`; `make test` / pytest is **4 passed**; history shows the feature committed on top
  of the scaffolded skeleton.

### Two real traps found and fixed en route (honest notes)

- The scaffold's **AGENTS.md is candidate-authored** at the skeleton commit, so `test_cycle` correctly
  refuses to trust it for command authority and inference then failed (`runtime_unavailable` /
  `command_discovery_blocked`). The fix: supply the verification command explicitly to the skeleton
  check via a raw `test_run`, which is exactly the G3 intent ("document the verification command so
  test_cycle works from run one") — the harness, bootstrapping the project, establishes it.
- The raw verifier only accepts a fixed set of **entrypoints** (`pytest`, `make`, `gradle`, `npm`, …) —
  **not** `python3`. So the verification command is `make test` (with the scaffold producing a Makefile
  whose `test` target runs pytest), which is robust across stacks.

## Tests

`workers/tests/test_stages.py`: `bootstrap_outcome` verdicts (SKELETON_NOT_GREEN / SKELETON_READY /
BOOTSTRAPPED / FEATURE_INCOMPLETE), the worker's `none`-feature handling, and `bootstrap_init` making
an empty dir git-ready. Full suite green (1616 passed, 3 skipped); invariant audit clean.

## Recommendation

**Phase 5 complete → proceed to Phase 6 (program scale & lasting memory: `cc_program` G4 +
`repo_memory` G5).** A greenfield idea now becomes a tested, extensible project with its first feature,
ready for the team cycle.
