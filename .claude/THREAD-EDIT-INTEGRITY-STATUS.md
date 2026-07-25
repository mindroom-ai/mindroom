# Thread edit integrity status

## Branch and pull request

- Branch: `fix/thread-edit-integrity`.
- Pull request: https://github.com/mindroom-ai/mindroom/pull/1641.
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Rejected frozen head: `abb8d4292672c91c4cb551772d214cdca54378e0`.
- Current local, remote, and GitHub PR head: `577d64559eba2ab2e5ce2a973e0a12f3af47c946`.
- Current production code head: `c0552cf5a3e7ad6a535f721623e7ee2cf2b7026a`.
- Never merge this pull request.
- Never amend or force-push.

## Current gate state

- Exact-head GitHub pytest failed `12` tests on `b39029c76e06656d53aced0f921503212cd2bfad`; that candidate is invalid.
- All independent approvals are stale and non-gating after the review-fix commits.
- Real-Tuwunel has not run.
- The heavy slot is currently owned by PR #1646's exact real-Tuwunel gate.
- Every approval, CI result, and live gate before the next pushed head is invalid.

## Verified blockers

- Thread-cache certification incorrectly required raw non-message interaction events to parse as visible room messages, forcing a homeserver refill instead of preserving them as raw-only members.
- Public cache writes now normalize their tuple-key event ID into the payload, so two old tests no longer created poisoned rows and instead asserted stale fallback behavior.
- The grouping helper test expected a payload event ID to override its authoritative tuple key, contrary to the corrected storage contract.
- A state root is rejected as a missing visible root, while an explicit wrong-room row is rejected earlier by the backend authoritative-index boundary and therefore has no later resolver diagnostic.
- Raw backend regressions must prove thread room-scope and point, recent, snapshot, and edit identity poison all fail closed without relying on public-write normalization.

## Required next steps

- Thread certification now requires a visible non-state root and valid relation-capable message members while preserving other interaction families as raw-only cache members.
- Public-write tests now exercise malformed message content only; raw SQL corruption owns event-ID mismatch coverage.
- Raw backend coverage now poisons a thread root's explicit room plus point, recent, snapshot, and latest-edit payload identities.
- The exact local SQLite replay for all affected contracts passes `14` tests with no fanout.
- Focused Ruff lint and formatting pass on all changed files.
- PostgreSQL poison and failed-test replay remain queued behind PR #1646.
- Current candidate production source diff is `+916/-390`, net `+526` against the exact merge base.
- The narrow certification correction is complete, and no more blocker patches may accumulate before simplification.
- Re-run exact failed files, owning cache suites, full pytest, Tach, and all-file pre-commit under resource ownership.
- Push small follow-up commits after verifying Git author.
- Refresh the PR body and all campaign evidence for the new exact head.
- Remove this file only when a new exact head is frozen.
- Run fresh exact-head native Codex and Claude `opus` xhigh reviews after every code commit sequence.
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

- The published branch remains `577d64559eba2ab2e5ce2a973e0a12f3af47c946`, and the current worktree contains the uncommitted source-minimal reset.
- `src/mindroom/matrix/replacements.py` is the 76-line replacement-domain owner for bundled flattening, identity, scope, relation validity, canonical ordering, and content projection.
- `event_info.py` is restored to relation facts plus the small room and state helpers.
- Cache edit lookup now receives the full original event and a surface validator, while one cache-row decoder validates durable payload identity.
- SQLite and PostgreSQL latest-edit SQL now owns only cache scope, joins, and canonical event-ID collation; shared Python owns Matrix validity and malformed-newest fallback.
- Full history, bundled preview, point lookup, snapshots, approval lookup, sidecar hydration, and cleanup consume the shared candidate seam.
- The custom encrypted-media and Base64 validator, compatibility re-exports, duplicate selectors, duplicate projections, and schema-v4 migration are removed.
- PostgreSQL schema v3 intentionally retains `idx_mindroom_event_cache_event_edits_room_original_ts` as the single narrowing index because explicit query-level `COLLATE "C"` owns equal-timestamp correctness.
- Production source is currently net `+198` against merge base `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`, satisfying the hard `<= +200` gate.
- Ruff formatting, Ruff lint, `ty`, and diff checks pass for every dirty Python file.
- The exact non-PostgreSQL prior-CI files pass `15`, `13`, `18`, and `4` tests.
- Full `tests/test_event_cache.py` and focused thread, approval, and cache-contract regressions pass.
- That focused cache run unexpectedly exercised the available PostgreSQL parametrization while PR #1646 owned the heavy slot, so no further PostgreSQL, full-suite, hook, or live work may start until explicit release.
- The next safe step is to update external evidence, commit locally after author verification, and wait for resource ownership before the required exact PostgreSQL replay and push.
