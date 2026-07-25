# Thread edit integrity status

## Branch and pull request

- Branch: `fix/thread-edit-integrity`.
- Pull request: https://github.com/mindroom-ai/mindroom/pull/1641.
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Rejected frozen head: `abb8d4292672c91c4cb551772d214cdca54378e0`.
- Current production source-reset head: `6a69cfd6daa88880b047c0675148612cb5ac4003`.
- Rejected review head: `fae7ddad0b5242396565b2069a439875718d12d5`.
- Current pushed branch and production code head: `83442e9914ced7cf02f8915d75c0f63131b92b4d`.
- Never merge this pull request.
- Never amend or force-push.

## Current gate state

- Fresh native Codex xhigh and Claude Opus 5 high both returned `CHANGES REQUIRED` on exact `fae7ddad0b5242396565b2069a439875718d12d5`.
- Follow-up commits `8fbc4b033` and `83442e991` fix the verified blockers and invalidate every review and CI result from `fae7ddad0`.
- GitHub CI on the new pushed head is pending.
- Real-Tuwunel has not run.
- PR #1646 owns the heavy resource slot, and PR #1641 must not run full pytest, PostgreSQL fanout, Docker, all-file hooks, or live validation.
- Every approval, CI result, and live gate before the next pushed head is invalid.

## Verified blockers

- Cached latest-edit lookup could accept an invalid explicit row when the original carried any valid bundled replacement.
- PostgreSQL latest-edit fallback used an unbounded client-side cursor, and recent-event `LIMIT` could let numeric JSON poison hide later valid rows.
- Wrong-room and state events could create sidecar ownership, and `/threads` could expose wrong-room or state roots.
- Tach omitted the replacement owner and retained stale interface names.
- Event-cache security and storage docs omitted the shared replacement-validity contract.
- Bounded stale-stream cleanup lacked an explicit edit-only missing-original regression.

## Required next steps

- Cache lookup now validates the explicit row itself, combines it with bundled candidates only after validation, and preserves malformed-newest fallback.
- PostgreSQL latest-edit and recent-event fallback use server-side cursors, while canonical equal-timestamp ordering remains explicit `COLLATE "C"`.
- Sidecar ownership and `/threads` roots reject state events and explicit room conflicts.
- Bounded stale-stream cleanup proves edit-only history does not synthesize a visible original.
- Focused replacement, approval, thread-page, stale-cleanup, Ruff, formatting, commit-hook, and Tach checks pass.
- One focused selector unintentionally expanded to PostgreSQL parametrizations while the resource slot was unavailable; all `37` cases passed, and no further PostgreSQL work may run before ownership.
- Current pushed production source diff is `+693/-503`, net `+190` against the exact merge base.
- Commit and push Tach, docs, and this handoff, then refresh the PR body and campaign evidence for the exact new head.
- Re-run the owning cache suites, full pytest, all-file pre-commit, and real-Tuwunel only under resource ownership.
- Remove this file only when a new exact head is frozen.
- Run fresh exact-head native Codex and Claude with explicit `--model=claude-opus-5 --effort=high` after every code commit sequence.
- Start the fresh Claude review only when an Opus slot is free.
- Run real-Tuwunel only after both fresh reviews approve the same unchanged head.

## Design and source-minimality reset

- Independent source review of exact production head `c0552cf5a3e7ad6a535f721623e7ee2cf2b7026a` is `CHANGES REQUIRED`.
- Exact c055 growth is concentrated in `event_info.py` at net `+282`, cache common code at net `+105`, read projection at net `+58`, backend parity at net `+41`, and approval, snapshot, and tool plumbing at net `+40`.
- Restore `event_info.py` to relation facts and small room/state helpers.
- One approximately 70-80 line `matrix/replacements.py` must own bundled flattening, identity/scope/relation validity, canonical `(origin_server_ts, event_id)` ordering, and content merge.
- History, bundled preview, point lookup, snapshots, approval, and cleanup must consume that candidate API.
- Cache edit lookup must accept its owning surface validator, with message validity delegated to nio/media parsing and approval validity delegated to approval parsing.
- Keep one cache-row decoder; SQLite and PostgreSQL own only SQL plus PostgreSQL bytewise `COLLATE "C"` ordering.
- Delete the custom encrypted-media/Base64 parser, duplicate selectors/projections/cache predicates, and compatibility re-exports.
- Preserve same-sender/type, non-state, room-scope, non-synthesized edit-of-edit, malformed-newest fallback, canonical tie-break, immutable original timestamp/relation, visible activity, cache-index, bundled/explicit, media, approval, and raw-only interaction invariants.
- Hard production target is net `<= +200` lines against the exact merge base, ideally net `+160..+190`, requiring approximately 330-365 lines of deletion from c055.
- No PostgreSQL fanout, full pytest, independent review, or live gate may start before the simplified source target and focused regressions pass.

## Active source-minimal reset

- The source-minimal reset and review corrections are published through `83442e9914ced7cf02f8915d75c0f63131b92b4d`.
- `src/mindroom/matrix/replacements.py` is the 76-line replacement-domain owner for bundled flattening, identity, scope, relation validity, canonical ordering, and content projection.
- `event_info.py` is restored to relation facts plus the small room and state helpers.
- Cache edit lookup now receives the full original event and a surface validator, while one cache-row decoder validates durable payload identity.
- SQLite and PostgreSQL latest-edit SQL now owns only cache scope, joins, and canonical event-ID collation; shared Python owns Matrix validity and malformed-newest fallback.
- Full history, bundled preview, point lookup, snapshots, approval lookup, sidecar hydration, and cleanup consume the shared candidate seam.
- The custom encrypted-media and Base64 validator, compatibility re-exports, duplicate selectors, duplicate projections, and schema-v4 migration are removed.
- PostgreSQL schema v3 intentionally retains `idx_mindroom_event_cache_event_edits_room_original_ts` as the single narrowing index because explicit query-level `COLLATE "C"` owns equal-timestamp correctness.
- Production source is currently `+693/-503`, net `+190` against merge base `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`, satisfying the hard `<= +200` gate.
- Ruff formatting, Ruff lint, `ty`, and diff checks pass for every dirty Python file.
- The exact non-PostgreSQL prior-CI files pass `15`, `13`, `18`, and `4` tests.
- Full `tests/test_event_cache.py` and focused thread, approval, and cache-contract regressions pass.
- That focused cache run unexpectedly exercised the available PostgreSQL parametrization while PR #1646 owned the heavy slot, so no further PostgreSQL, full-suite, hook, or live work may start until explicit release.
- No durable live instructions, worktrees, handoffs, or evidence may use a temporary directory.
- The next safe steps are to push this status-only update, refresh PR evidence, inspect current AI feedback, and start fresh exact-head reviews only when an Opus slot is free.
- PostgreSQL, full pytest, all-file hooks, Docker, and real-Tuwunel remain prohibited until explicit resource ownership.
