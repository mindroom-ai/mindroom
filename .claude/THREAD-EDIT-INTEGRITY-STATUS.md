# Thread Edit Integrity Status

## Current candidate

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`
- Branch: `fix/thread-edit-integrity`
- Invalidated head: `18a8633999989645b59d5afafd82168c08ef23a0`
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`
- Never merge this PR from the agent workflow.

## Fresh exact-head blocker

- Fresh native Codex review reproduced cross-room replacement application at `18a863399`.
- `_resolve_thread_history_from_event_sources_timed(..., room_id="!expected")` accepts a same-sender edit carrying `room_id="!other"` when the room-scoped original omits `room_id`.
- Bundled thread-root validation has the same gap because it compares payload room IDs only when both are present and does not use the caller's authoritative room ID.
- CodeRabbit's stale-head note about `FakeEventCache.get_latest_edit()` is also correct: the test fake still breaks equal timestamps by insertion order instead of event ID.
- Both native Codex reviews and the isolated Fable review were stopped.
- All approvals, CI interpretation, and live-gate preparation at `18a863399` are invalid for the next head.

## Required next work

- Full-history application now rejects an edit whose explicit room ID differs from the authoritative caller room, even when the original payload omits `room_id`.
- Bundled replacement validation now receives the caller room and covers the missing-original-room shape.
- The approval test fake now uses `(origin_server_ts, event_id)` ordering.
- Run focused owning tests, relevant SQLite/PostgreSQL backend tests, full pytest, Tach, and all-file pre-commit.
- Verify `Bas Nijholt <bas@nijho.lt>` before every commit; commit and push follow-ups without amend or force-push.
- Refresh the PR body and all campaign evidence if behavior or validation text changes.
- Remove this handoff only after implementation and local validation finish.
- Restart fresh Codex, fresh Fable, CI/AI review, and exact-head real-Tuwunel validation on the new frozen head.

## Prior exact-head evidence

- `18a863399` passed full pytest: `11522 passed`, `54 skipped`, `31 warnings`.
- `18a863399` passed explicit Tach and all-file pre-commit.
- Those local results remain historical evidence only; the next code commit requires new exact-head validation.
- The real-Tuwunel gate did not run at `18a863399`.
- The direct production-seam reproduction changed from forged output to the original body, and bundled validation changed from `True` to `False`.
- New focused regressions plus all approval-manager tests pass: `115 passed`.

## Preserved local files

- `.claude/TASK-1784907055-a461.md`
- `.claude/TASK-1784912547-27b4.md`
- `.claude/TASK-1784915037-90d6.md`
