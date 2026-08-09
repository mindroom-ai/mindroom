# Bounded hydration installation blocker

Base commit: `81191c6ad44b18c9e7a93c10245e557aa1cc92b6`.

Fix the strict-export writer blocker: a cold hydration currently projects its entire event tuple inside one `Backend.write()` transaction. SQLite held the writer for about 32 seconds at 300,000 events and about 121 seconds at 2,000,000 events while concurrent live admission has a 10-second busy timeout.

Requirements:

- Use test-driven development and record the exact RED command and failure before production edits.
- Preserve the one-million-message product ceiling and strict-export semantics.
- Install projected events in hard-bounded chunks, with a small production constant justified by tests.
- Every chunk must claim/materialize and verify the expected membership epoch in its own transaction.
- Do not publish conversation hydration/completeness or attempted-policy rank until one final transaction.
- For room-history recovery, do not settle the exact recovery obligation until that same final transaction.
- Crash, cancellation, busy failure, or injected failure after a middle chunk may leave idempotent partial projection rows, but must leave the hydration marker/recovery obligation retryable.
- A membership fence between chunks must delete/supersede earlier rows; later chunks and the final marker must refuse the stale epoch.
- Retry must be idempotent on both SQLite and PostgreSQL.
- Nonblocking readers may observe locally projected partial rows under their existing contract; strict readers must not trust them without the final marker.
- Avoid staging/swap generations and avoid lowering the export ceiling.
- Do not hold the complete walk in any single write transaction.
- Include focused tests for ordinary hydration, history recovery, failure after a middle chunk, cancellation/retry, and membership fence between chunks on both backends.
- Include a transaction-size/count assertion that mutation-kills a return to one giant write.
- Run `uv sync --all-extras` in this fresh worktree before verification.
- Run focused tests, Ruff, format check, ty for touched files, Tach if boundaries change, and `git diff --check`.
- Self-review the full base-to-head diff, commit the change, and leave the worktree clean.

Write the implementation report to `.superpowers/sdd/pr-landing-blockers/export-report.md` with: RED evidence, design, files changed, verification commands/results, production/test LOC, measured or structurally proven transaction bound, and remaining concerns.
