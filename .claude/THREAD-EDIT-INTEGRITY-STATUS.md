# Thread edit integrity handoff

## Current state

PR #1641 remains open and must never be merged by this worker.

Exact candidate `c8f31fc332b2febcf9f2e3aa1652b0cf06be5faa` is rejected by fresh independent Codex review.

Current CI for that rejected head is green, but all review and live gates are invalid.

The isolated Claude Fable 5 xhigh review approved rejected head `c8f31fc332b2febcf9f2e3aa1652b0cf06be5faa`.

That approval is retained as non-gating evidence because it missed blockers reproduced by the independent Codex and source-minimality audits.

The real-Tuwunel gate has not run.

## Follow-up implementation in progress

- Shared replacement projection now replaces content wholesale, drops replacement `m.relates_to`, preserves the original relation, and preserves the original timestamp and thread membership.
- Bundled replacements now require non-empty sender/type/event IDs, a timestamp, a valid replacement envelope, and renderable message content.
- Bundled candidates now use canonical `(origin_server_ts, event_id)` newest-first ordering for previews and full history.
- Cold scans reject state and explicit wrong-room rows before root accounting; sidecar hydration rejects them before owner registration.
- Cache certification rejects explicit wrong-room rows and requires a parseable non-state, non-edit root.
- Generic visible resolution and both thread-preview tools now receive authoritative room scope.
- Approval parsing rejects state events and explicit room mismatches.
- Latest-edit APIs now require sender and event type; SQLite and PostgreSQL reject malformed event envelopes before ordering.
- PostgreSQL remains schema v3 with one legacy narrowing index.
- Query-level `edit_event_id COLLATE "C" DESC` is the correctness owner for bytewise equal-timestamp ties, so no second collation-specific index is created.

The reproduced failure subset passes on SQLite and PostgreSQL.

Exact new regressions now cover:

- Full projection preserves original timestamp, thread/reply relation, and order while dropping stale rich fields.
- Cached point and SQLite/PostgreSQL snapshot projection preserve original relation/timestamp and drop stale fields.
- Bundled preview and full resolution share timestamp/event-ID winner selection.
- Bundled candidates missing sender, type, or timestamp are rejected by preview and full resolution.
- Cold state/wrong-room roots remain missing and cannot write cache snapshots.
- Poisoned state/wrong-room cached roots force authoritative refill.
- Wrong-room originals cannot enter sidecar owner registration.
- SQLite/PostgreSQL lookup, point, and snapshot paths fall back past newest envelopes with missing sender, wrong type, missing body/msgtype, mismatched event ID, or wrong relation target.
- Generic visible resolution rejects state and cross-room originals.
- Approval state originals fail both original-card detection and typed parsing.

The focused new regression selection passes.

The first broad touched/backend run passed every behavior test and exposed one stale schema-v4 assertion.

That assertion and the current-version maintenance probe now expect retained schema v3.

The full storage-maintenance file and Tach pass after the correction.

A clean broad rerun of the twelve owning, cache-backend, approval, tool-preview, and stale-cleanup files passes at 100%.

Current pushed implementation head is `b241f9874a7e001028fee6006d1895641390bd9c`.

Full pytest, a final Tach rerun, all-file pre-commit, fresh exact-head reviews, CI, and real-Tuwunel remain pending.

The first full-suite rerun exposed thirteen stale test-contract failures and passed the other `11550` tests.

Six snapshot cases now expect preserved original timestamps and relations, two media cases expect invalid full-history events to be rejected, and the cache fuzzer supplies the newly required sender/type lookup identity.

The fuzzer tracks actual edit targets so its PostgreSQL fanout remains below the timeout while preserving the latest-edit invariant.

All three affected files, including every exact CI failure, pass with `47 passed`.

Published follow-up head `51a083380c143073d8ef25b90341f3588aa9f35c` has green GitHub pytest.

Tach passes.

All-file pre-commit passes on its second run.

The same seven unrelated frontend Prettier rewrites were reversed exactly and are absent from the branch.

A local full rerun passed `11561` tests and had only an unrelated knowledge Git-status flake plus a PostgreSQL 45-thread fanout timeout while multiple repository-wide suites shared Docker/PostgreSQL.

The exact knowledge test passes alone.

The PostgreSQL fanout and full suite must rerun after the concurrent repository tests finish; do not increase the timeout or weaken the invariant.

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
