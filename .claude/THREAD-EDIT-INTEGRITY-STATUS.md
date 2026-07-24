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

- Cached `m.room.message` selection now requires a string body in `m.new_content`, while custom approval-card edits remain supported.
- SQLite and PostgreSQL point-event and snapshot fallback regressions pass for a newest empty-object replacement.
- Bundled replacements are now validated at the shared thread-root preview seam for sender, type, target, state, room, and edit-of-edit rules.
- Corrected bundled-preview fixtures now use same-sender originals and replacements.
- The production PostgreSQL query now has an exact regression against an isolated ICU `und-x-icu` edit-event-ID column.
- Run targeted tests, backend suites, full pytest, Tach, and all-file pre-commit after the final code commit.
- Commit and push small follow-ups with `Bas Nijholt <bas@nijho.lt>` verified before each commit.
- Update the PR body and both external campaign notes.
- Remove this handoff before declaring a new stable candidate.
- Restart fresh Codex, Fable, CI/AI, and real-Tuwunel validation from the exact new head.

## Current test evidence

- Cache malformed-fallback and approval-card focus: `8 passed` across SQLite and PostgreSQL.
- Shared preview and both Matrix tool owning files: all `188` tests passed.
- PostgreSQL C-collated index and ICU-discriminating production lookup: `2 passed`.
- Broad owning/backend matrix passed before the schema-version follow-up.
- First broad full-pytest attempt had one 60-second PostgreSQL fanout timeout under four-worker contention; the branch changed afterward, so every full-suite result must be rerun.
- Schema v4 now creates the C-collated index and drops the locale-dependent legacy index only while upgrading v1-v3.
- The v3 migration regression proves the final schema contains only the C-collated latest-edit index.
- PostgreSQL v1/v2/v3 migrations, ICU ordering, current-version lock behavior, and all thread-resolution reuse tests pass (`33 passed`).

## Completed invalid-head reviews

- Both native Codex reviews rejected `bf7a963dc629910583d6ce0c6920e532bd09b233`.
- Fable rejected the same exact head after independently reproducing the empty-object cache fallback mismatch.
- Fable treated bundled preview validation and the ICU query regression as pre-existing test-strength issues, but both remain required by the user's stricter final gate.

## Preserved local files

- `.claude/TASK-1784907055-a461.md`
- `.claude/TASK-1784912547-27b4.md`
- `.claude/TASK-1784915037-90d6.md`
