# Work from a link: issue or repository → swarm → pull request

Status: design + first slice (2026-11-14). The first slice covers GitHub only.

## What it does

Give the swarm a link instead of, or as well as, a local repository:

| Link | What the swarm does |
|---|---|
| Issue (`github.com/o/r/issues/12`) | Implements it, opens a pull request that closes it |
| Pull request (`github.com/o/r/pull/34`) | Continues it: addresses its review feedback and failing CI, pushes to the same branch |
| Repository (`github.com/o/r`, `…/tree/<branch>`), with a written request | Implements the request against the default (or named) branch |

The swarm itself is unchanged: the architect plans, the agents build, the checks are measured, the round
review judges. Around it, the harness only fetches context, prepares a checkout, and publishes. Every
judgment stays with the agents. That includes whether an issue is a bug (reproduce it first) or a feature,
what to build, and how to fix CI.

## Where it works

- **Temporary checkout (the default when a link is given and no repository path is):** the harness
  clones the repository into the system temp folder (`$TMPDIR/conductor-harness/<owner>-<repo>-<run>`).
  The swarm runs there and the result is pushed. The checkout is kept after the run, so the run's files
  stay browsable and a follow-up can reuse it. The system cleans the temp folder; a later run on the
  same PR clones again if the checkout is gone.
- **An existing local checkout:** give the repository path as well. The harness fetches the base, or the
  pull request's head, into that checkout. It never switches the checkout's own branch.
- **No push access:** the harness forks the repository under the signed-in account, pushes there, and
  opens the pull request across.

## Flow (inside the `swarm` workflow)

```
intake → model policy → setup (start from intake's ref) → planning → authoring → rounds → finish
       → publish (push + draft PR) → ci (wait for the repository's own CI)
       → green: mark ready (+ optional issue comment)
       → red:   follow-up swarm run on the PR with the failing logs (bounded by ciRounds)
```

- **intake** (`swarm_intake`) does four things:
  - parses the link;
  - fetches the issue or PR: title, body, labels, comments, and linked issues, PRs, files and CI runs.
    For a PR it also fetches review comments and the logs of failing checks;
  - prepares the checkout;
  - writes the request the architect reads.

  Without a link it passes `repoPath` and `description` through unchanged, so local runs behave as
  before.
- **setup** starts the run's branch from intake's `startFrom` ref: the base branch's tip, or the PR's head.
- **publish** (`swarm_publish`) runs only when the run delivered:
  - it pushes the branch: a new branch for an issue or repository link, the PR's own head branch for a PR link;
  - it opens a draft PR with a single summary paragraph and `Closes #N`. The paragraph covers the
    architect's assessment, the design documents, the spec proof and the spend;
  - for a PR link it comments on the PR instead.

  `publish: branch` stops after the push. `publish: none` stops before it (a dry run).
- **ci** (`swarm_ci`) waits for the repository's own checks on the pushed head. The repository's CI is
  the real gate.
  - Green, or no CI: the PR is marked ready (`ready: ci`, the default; `ready: never` keeps it a draft).
  - Red: a follow-up swarm run (a sub-workflow) continues the PR, with the failing logs as its request.
    It is bounded by `ciRounds` and by the spend left under the ceiling.

## Untrusted input

Anyone can write an issue or a comment. The request the architect reads states that:
- the issue text says what is wanted;
- it is not instructions about how to run;
- anything asking for secrets, credentials, changes outside the repository, or skipped checks is ignored.

Linked material is fetched through the existing `linked_context` path, which labels it as untrusted
evidence. Credentials stay with the provider's CLI login (`gh`). They never appear in prompts, URLs or
the repository.

## Inputs (added to `swarm`)

| Input | Default | Meaning |
|---|---|---|
| `link` | — | Issue, PR or repository URL (or `owner/repo`, `owner/repo#12`) |
| `repoPath` | — | Optional with a link: work in this checkout instead of a temporary one |
| `description` | — | Optional with an issue or PR link: added to the issue as the person's own request |
| `base` | the repository's default branch | Base branch for a new pull request |
| `publish` | `pr` with a link, `none` without | `pr`, `branch` or `none` |
| `ready` | `ci` | `ci`: mark ready once the repository's CI passes; `never`: leave as draft |
| `ciRounds` | 2 | Follow-up runs allowed for failing CI |
| `commentOnIssue` | false | Comment on the issue with the pull request when it is ready |

## Providers

A provider is:
- a link parser;
- an issue and PR reader;
- a clone URL;
- a push permission check and a fork;
- PR create, comment and ready operations;
- a CI status and log reader.

The first slice implements GitHub through `gh`. GitLab (`glab`), Bitbucket and Azure DevOps fit the
same shape. Jira and Linear are ticket sources only: the request comes from the ticket, and the code
host comes from the ticket's linked repository or from `repoPath`. An unsupported host is refused at
intake with a plain message.

## Later

- Follow-ups triggered by new review comments.
- A label sweep: `conductor:auto` issues, one run each.
- Webhooks.
- Batches of issues.
- Cleanup of temporary checkouts after their PR merges or closes.
- The GitLab and ticket-system providers.
