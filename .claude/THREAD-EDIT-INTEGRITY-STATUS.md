# Thread edit integrity status

## Branch and pull request

- Branch: `fix/thread-edit-integrity`.
- Pull request: https://github.com/mindroom-ai/mindroom/pull/1641.
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Rejected frozen head: `abb8d4292672c91c4cb551772d214cdca54378e0`.
- Current local, remote, and GitHub PR head: `c0552cf5a3e7ad6a535f721623e7ee2cf2b7026a`.
- Never merge this pull request.
- Never amend or force-push.

## Current gate state

- Exact-head GitHub pytest failed `12` tests on `b39029c76e06656d53aced0f921503212cd2bfad`; that candidate is invalid.
- All independent approvals are stale and non-gating after the review-fix commits.
- Real-Tuwunel has not run.
- PR #1646 owns the heavy resource slot; mindroom-nio PR #20 and PR #1639 are ahead of PR #1641.
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

- Production growth is concentrated in replacement-domain helpers at net `+282`, durable cache seams at net `+180`, thread resolution and certification at net `+39`, visible point and full projection at net `+19`, approval projection at net `+1`, and call-site wiring at net `+5`.
- `event_info.py` remains the single source of truth for original scope, replacement validity, bundled extraction, and `(origin_server_ts, event_id)` selection.
- `client_visible_messages.py`, point projection, snapshots, and cache-row validation must consume that domain seam instead of repeating sender, room, state, content, or ordering predicates.
- Strict encrypted-media validation should use compact structural parsing while retaining exact Matrix v2, JWK, URL-safe Base64, decoded-size, and MXC constraints.
- Cached index validation should keep one backend-neutral decoder and remove repeated row-shape and candidate-validity plumbing from backend consumers.
- Bundled latest and ordered-candidate APIs should share one valid-candidate sequence instead of filtering the same input twice.
- The simplification target is at most net `+400` production lines against the exact merge base, deleting at least `126` net lines without weakening any regression.
- No PostgreSQL fanout, full pytest, independent review, or live gate may start before the simplified source target and focused regressions pass.
