# Thread edit integrity handoff

## Current state

PR #1641 remains open and must never be merged by this worker.

Exact candidate `c8f31fc332b2febcf9f2e3aa1652b0cf06be5faa` is rejected by fresh independent Codex review.

Current CI for that rejected head is green, but all review and live gates are invalid.

The isolated Claude Fable 5 xhigh review of the rejected head is still running and its result must be retained as non-gating evidence.

The real-Tuwunel gate has not run.

## Verified blockers

- Cold room scans admit explicit wrong-room and state events before root accounting, grouping, cache certification, and sidecar owner registration.
- Cached snapshot validation is not room-aware and can trust already-poisoned root rows.
- Thread-root preview callers omit the authoritative room ID.
- Generic visible-message resolution admits state originals and lacks caller-room filtering for stale cleanup.
- Replacement projection overwrites original relations, merges stale fields on cached reads, changes thread membership, and replaces the original timestamp.
- Bundled previews choose structural candidate order instead of canonical `(origin_server_ts, event_id)` order.
- Bundled validation allows missing sender and type identities.
- Approval-card parsing accepts state originals.
- Raw preview/cache replacement paths need the same complete event/renderability validity that parsed full history enforces.

## Justified simplifications

- Require `sender` and `event_type` on every latest-edit cache lookup and delete unused optional SQL modes plus duplicate private loaders.
- Keep PostgreSQL schema version 3 and its existing narrowing index.
- Retain query-level `edit_event_id COLLATE "C" DESC`, which is the correctness owner for bytewise ties.
- Document retaining the legacy index because the query sorts only equal-timestamp candidates bytewise; do not create a silent second index.

## Required tests

- Wrong-room and state roots remain missing during cold scan and cannot certify cached empty history.
- Wrong-room originals cannot register sidecar ownership under the caller room.
- Cached poison rows are rejected.
- Full, cached point, and SQLite/PostgreSQL snapshot projections preserve the original relation and timestamp while dropping stale fields.
- Edit content cannot move a message between threads or reorder visible history.
- Bundled preview and full history choose the same timestamp/event-ID winner.
- Thread tool previews pass authoritative room scope.
- Generic stale-cleanup resolution rejects state and cross-room inputs.
- Missing sender/type bundled candidates are rejected.
- Approval state originals are rejected before cached edits are applied.
- SQLite/PostgreSQL latest-edit APIs require sender/type and preserve malformed-newest fallback.

## Validation and workflow

Run focused owning tests, both parametrized backends, full pytest, Tach, and all-file pre-commit.

Verify Git author is `Bas Nijholt <bas@nijho.lt>` before every commit.

Use small follow-up commits, push frequently, never amend, and never force-push.

After the final handoff-removal commit, restart fresh Codex and Fable reviews on one exact head.

Only after both approve may the real-Tuwunel harness be repinned and run from scratch on that same exact head.

Any new commit invalidates all review, CI, and live evidence.

## Worktree preservation

The following pre-existing untracked prompts must remain uncommitted:

- `.claude/TASK-1784907055-a461.md`
- `.claude/TASK-1784912547-27b4.md`
- `.claude/TASK-1784915037-90d6.md`
