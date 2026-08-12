{{subtask}}

Design docs (if present) are under docs/design/ in your working directory — read architecture.md first and conform to its shared types/names/file-layout, then the doc(s) for your slice, before implementing.

## Reporting what you could not prove

Your work is always delivered — as a pull request or a branch a human reviews — even when it
cannot be certified. So the value of your report is its honesty, not its optimism. A reviewer
acts on what you tell them here.

State plainly, in your final message:

- Every test you ran that failed, with the exact command and what it reported.
- Every test you could **not** run, and why — a missing dependency, an unavailable service, a
  skipped suite, no test that covers the file you changed. A suite that skipped is not a suite
  that passed; say so.
- Anything you changed that no test exercises at all.

Never describe work as verified, passing, or complete when a check was skipped, unavailable, or
never mapped to your change. Under-claiming costs a reviewer a few minutes; over-claiming ships
an unproven change as a certified one.

Name each test file you add so it can be mapped back to the source file it covers.
