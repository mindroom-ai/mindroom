# Thread Edit Integrity Status

## Current candidate

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`
- Branch: `fix/thread-edit-integrity`
- Invalidated head: `bf7a963dc629910583d6ce0c6920e532bd09b233`
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`
- Never merge this PR from the agent workflow.

## Fresh review blockers

- SQLite and PostgreSQL latest-edit queries accept `m.new_content: {}` and let that newest unrenderable message edit mask an older valid edit.
- Cached point-event reconstruction then falls back to the original body.
- Cached agent-message snapshots instead expose the empty replacement content and newest timestamp.
- Bundled thread-root replacements are rendered without validating sender, event type, target event ID, state-event absence, or whether the original is itself an edit.
- Existing bundled-preview tests include invalid cross-sender replacements and must be corrected.
- PostgreSQL has C-collated production query/index code, but the regression only inspects index DDL instead of executing the query against a non-C event-ID column collation.
- The PR body describes older candidate-selection behavior and must be updated after the implementation stabilizes.

## Required next work

- Make cached `m.room.message` selection require a renderable string body in `m.new_content`, while preserving custom approval-card edits.
- Add SQLite and PostgreSQL regressions for point-event and snapshot fallback from a newest empty-object replacement.
- Validate bundled replacements at the shared thread-root preview seam and add sender/type/target/state/edit-of-edit regressions.
- Execute the production PostgreSQL query against an isolated ICU-collated edit-event-ID column and prove bytewise Matrix ordering.
- Run targeted tests, backend suites, full pytest, Tach, and all-file pre-commit.
- Commit and push small follow-ups with `Bas Nijholt <bas@nijho.lt>` verified before each commit.
- Update the PR body and both external campaign notes.
- Remove this handoff before declaring a new stable candidate.
- Restart fresh Codex, Fable, CI/AI, and real-Tuwunel validation from the exact new head.

## Preserved local files

- `.claude/TASK-1784907055-a461.md`
- `.claude/TASK-1784912547-27b4.md`
- `.claude/TASK-1784915037-90d6.md`
