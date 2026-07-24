# Thread edit integrity status

## Branch and pull request

- Branch: `fix/thread-edit-integrity`.
- Pull request: https://github.com/mindroom-ai/mindroom/pull/1641.
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Rejected frozen head: `abb8d4292672c91c4cb551772d214cdca54378e0`.
- Never merge this pull request.
- Never amend or force-push.

## Current gate state

- Fresh native Codex review returned `CHANGES REQUIRED` on exact head `abb8d4292672c91c4cb551772d214cdca54378e0`.
- Claude `opus` at xhigh remains active on the same exact head; its result becomes evidence-only after any code commit.
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
- Add deterministic full-resolution, point, snapshot, recent-room, SQLite, and PostgreSQL regressions at the owning seams.
- Keep full correctness validation in shared Python helpers and use only narrow SQL sender/type prefilters for bounded edit lookup.
- Re-run exact failed files, owning cache suites, full pytest, Tach, and all-file pre-commit under resource ownership.
- Push small follow-up commits after verifying Git author.
- Refresh the PR body and all campaign evidence for the new exact head.
- Remove this file only when a new exact head is frozen.
- Run fresh exact-head native Codex and Claude `opus` xhigh reviews after every code commit sequence.
- Run real-Tuwunel only after both fresh reviews approve the same unchanged head.
