# Thread Edit Integrity Status

## Current candidate

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`
- Branch: `fix/thread-edit-integrity`
- Invalidated head: `6b6b43e8f68916a04224d3c6b5ff5fd77c88dc80`
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`
- Never merge this PR from the agent workflow.

## Third review-round blockers

- Cached latest-edit SQL scopes the cache row to the caller room but ignores an explicit conflicting `room_id` inside edit JSON.
- A wrong-room edit stored under the authoritative room wins `get_latest_edit`, cached point reconstruction, and latest-agent snapshot selection on SQLite; PostgreSQL has the same query shape.
- A state event with type `m.room.message` can be inserted as a full-history original and then edited because original state validity is not checked.
- Cached point reads also apply edits to state originals, and agent snapshots treat state originals as visible messages.
- Full-history bundled replacement extraction returns the first `event` candidate before `latest_event`, while preview extraction enumerates `latest_event` first.
- A bundled payload containing an older valid `event` plus a newer valid `latest_event` therefore resolves to the older body.
- The exact probes at `6b6b43e8f` reproduced all three classes.

## Design reconsideration

- This is the third review round with new semantic gaps, so do not add another isolated workaround.
- Put state-event and explicit-room checks in shared event metadata helpers.
- Make cache edit indexing reject explicit other-room and state-event replacements at the backend-neutral owning seam.
- Keep read-time SQL room checks for previously persisted or malformed rows.
- Make cached point projection and snapshot scope use the shared state/room rules.
- Export one bundled candidate enumerator and validator from the visible-message seam.
- Delete the duplicate single-candidate full-history extractor and let full history validate and retain every bundled candidate before deterministic latest selection.

## Required next work

- Shared `event_info` helpers now own state-event and authoritative-room evidence.
- Cached latest-edit SQL rejects explicit other-room JSON on SQLite and PostgreSQL, including custom approval edits.
- Cached point projection refuses state originals and explicit room mismatches.
- Snapshot scope refuses state originals and explicit room mismatches.
- Full-history parsing excludes state originals.
- Full-history parsing also excludes originals carrying an explicit other room.
- Full history and preview now share bundled candidate enumeration and validation.
- Full history retains all valid bundled candidates for deterministic latest selection and fallback.
- Deterministic full-history tests cover state originals and dual bundled `event`/`latest_event` ordering.
- SQLite and PostgreSQL cache regressions cover explicit other-room edits for direct latest-edit lookup, cached point projection, latest-agent snapshots, and custom-event lookup.
- SQLite and PostgreSQL regressions ensure state originals are not edited or returned as agent snapshots.
- Run focused owning tests, relevant parametrized backend tests, full pytest, Tach, and all-file pre-commit.
- Verify `Bas Nijholt <bas@nijho.lt>` before every commit.
- Commit and push small follow-ups without amend or force-push.
- Remove this handoff only after local validation is complete.
- Restart fresh Codex, fresh Fable, CI/AI review, and real-Tuwunel validation on the next exact head.

## Current validation

- Exact reproduced probes now return no state original, the newer bundled `latest_event`, and the older valid in-room cached edit.
- The explicit other-room original regression passes.
- New focused regressions pass on both cache backends: `8 passed`.
- Broad full/cache/backend/reuse focus passes: `471 passed`.
- Explicit Tach dependency/interface validation passes.

## Preserved local files

- `.claude/TASK-1784907055-a461.md`
- `.claude/TASK-1784912547-27b4.md`
- `.claude/TASK-1784915037-90d6.md`
