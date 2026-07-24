# Thread Edit Integrity Status

## Current state

- PR: https://github.com/mindroom-ai/mindroom/pull/1641
- Branch: `fix/thread-edit-integrity`
- Invalidated head: `24d2174bd08150beba6a6e2a34e4fa4db3f0ae90`
- Current pushed head: `193f4c768`
- Merge is forbidden.
- The final real-Tuwunel gate must restart whenever the branch head changes.

## Reproduced blockers

- Full history synthesized a same-sender edit-of-edit as a visible message.
- Full history let a malformed newest replacement mask an older valid replacement.
- Cached point reads applied foreign-sender replacements.
- Cached point reads applied replacements with a different Matrix event type.
- Cached point reads let a malformed newest replacement mask an older valid replacement.
- PostgreSQL inherited database collation for equal-timestamp event-ID ordering.

## Implemented follow-up work

- Full-history resolution retains all candidates and tries them newest-to-oldest.
- Full-history resolution applies only same-sender candidates to a present original.
- Missing targets and edit-of-edit targets no longer synthesize visible messages.
- Full-history fix is committed and pushed as `193f4c768`.
- Cached latest-edit reads can scope by sender and event type.
- Cached latest-edit reads skip state events and replacements without object-valued `m.new_content`.
- PostgreSQL query and index ordering use bytewise `COLLATE "C"`.
- The new C-collated index uses a distinct name without dropping the legacy index during startup.

## Test evidence

- Full-history focused regressions: 7 passed.
- Full `tests/test_thread_history.py`: 84 passed.
- SQLite and PostgreSQL cached correctness plus PostgreSQL index regression: 9 passed.
- Full cache/backend/approval owning set: passed.
- PostgreSQL seeded concurrent cache trace and C-collated index regression pass together.
- Three unrelated shell timing failures from full-suite attempts pass five consecutive focused reruns each.

## Remaining gates

- Run the full owning test files.
- Run full pytest.
- Run Tach only if a governed boundary changed.
- Run all-file pre-commit.
- Commit and push atomic follow-up commits with the required author.
- Run fresh independent Codex review on the exact stable head.
- Run fresh Fable review on the exact stable head.
- Run the real-Tuwunel gate from `/tmp/pr1641-live-tuwunel.md` on the unchanged exact head.
- Verify current GitHub CI and every current review thread.
- Remove this file before declaring the PR merge-ready.
