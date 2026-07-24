# Thread edit integrity status

## Branch and pull request

- Branch: `fix/thread-edit-integrity`.
- Pull request: https://github.com/mindroom-ai/mindroom/pull/1641.
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Rejected frozen head: `abb8d4292672c91c4cb551772d214cdca54378e0`.
- Current pushed head before the active slice: `58ea95d689ca1b1aacf3c54c09635066d925486b`.
- Never merge this pull request.
- Never amend or force-push.

## Current gate state

- Fresh native Codex review returned `CHANGES REQUIRED` on exact head `abb8d4292672c91c4cb551772d214cdca54378e0`.
- Claude `opus` at xhigh approved the rejected head but is stale and non-gating.
- Real-Tuwunel has not run.
- PR #1641 does not own the heavy resource slot.
- Every approval, CI result, and live gate for `abb8d4292` is invalid after the next code commit.

## Verified blockers

- Encrypted media replacement validation accepts envelopes that violate Matrix `EncryptedFile` v2 and JWK requirements, so a malformed newest edit can mask an older usable edit and later fail decryption.
- Cached thread reads return payload JSON without checking its event ID against the joined authoritative index row.
- Recent-room reads return payload JSON without index validation, so a poisoned approval row can act on a different event ID.
- Thread snapshot ordering trusts stale `thread_events.origin_server_ts`, and snapshot validation does not compare payload time to the authoritative event row.
- Cached point and snapshot projection ignore valid bundled `unsigned.m.relations.m.replace` candidates that full history applies.
- Latest-edit queries validate sender and type only after materializing every edit candidate, allowing foreign-sender rows to make lookup unbounded.

## Required next steps

- Strict Matrix encrypted-file v2/JWK validation now rejects malformed newest replacements across shared selection, with focused full, SQLite, and pure-validator tests passing.
- Bundled and cached self-replacements are now rejected.
- Cache storage now makes the API event ID authoritative, and derived edit/thread indexes use the serialized index identity.
- Point, recent-room, full-thread, suffix-thread, and snapshot reads now validate payload ID and timestamp against authoritative event rows.
- Thread reads and snapshots order by the authoritative event-row timestamp instead of stale duplicated membership timestamps.
- Focused cache contracts, SQLite snapshots, recent reads, edit lookups, and durable thread-cache reuse pass; PostgreSQL execution remains resource-gated.
- Certified thread snapshots now reject malformed, duplicate, and canonically cross-thread child rows before resolution.
- Focused malformed-child and wrong-membership cases refetch authoritative history instead of silently omitting poisoned rows.
- The active slice gives cached point and snapshot projections the same bundled-plus-explicit `(origin_server_ts, event_id)` selection as full history.
- The active slice keeps full replacement validation in shared Python and adds only narrow sender and event-type SQL prefilters.
- Focused full-history and SQLite cache regressions pass `44` tests, focused file hooks pass, and Tach passes.
- Current source diff including the active slice is `+899/-391`, net `+508` against `origin/main`.
- Add deterministic full-resolution, point, snapshot, recent-room, SQLite, and PostgreSQL regressions at the owning seams.
- Keep full correctness validation in shared Python helpers and use only narrow SQL sender/type prefilters for bounded edit lookup.
- Re-run exact failed files, owning cache suites, full pytest, Tach, and all-file pre-commit under resource ownership.
- Push small follow-up commits after verifying Git author.
- Refresh the PR body and all campaign evidence for the new exact head.
- Remove this file only when a new exact head is frozen.
- Run fresh exact-head native Codex and Claude `opus` xhigh reviews after every code commit sequence.
- Run real-Tuwunel only after both fresh reviews approve the same unchanged head.
