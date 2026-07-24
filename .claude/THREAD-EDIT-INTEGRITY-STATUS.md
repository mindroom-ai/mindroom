# Thread edit integrity status

## Branch and pull request

- Branch: `fix/thread-edit-integrity`.
- Pull request: https://github.com/mindroom-ai/mindroom/pull/1641.
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Rejected frozen head: `5419f282ac065e3023b612ba6f8b45b3c64bbf13`.
- Never merge this pull request.
- Never amend or force-push.

## Current gate state

- Both fresh native Codex reviews returned `CHANGES REQUIRED` on exact head `5419f282ac065e3023b612ba6f8b45b3c64bbf13`.
- The exact-head Fable review returned `APPROVE`, but that result is non-gating because direct probes reproduced Codex blockers at the same head.
- The exact-head Fable report is preserved in the isolated `review-pr1641-fable-5419` worktree.
- Every review, CI, and live gate for `5419f282a` is invalid after the next code commit.
- Real-Tuwunel has not run.
- PR #1641 does not own the heavy resource slot.
- Nio PR #20 exact-head real-Tuwunel remains first in the resource queue.

## Verified blockers

- Full history rejects malformed standard-media edits through nio, while bundled preview and both cache backends accept the same missing-media-transport payload.
- A malformed newest approval replacement with no valid status masks an older terminal replacement and makes a resolved approval appear pending.
- Full history accepts a replacement with an empty event ID and applies it instead of falling back to an older valid replacement.
- A clock-skewed accepted edit can move visible activity time before the original timestamp and make a fresh stream eligible for stale cleanup.
- Cached point reads trust payload event IDs that disagree with the requested index key.
- Cached point and snapshot projection can apply valid edits to an invalid original `m.room.message` envelope, turning a nio `BadEvent` into visible text.

## Direct reproduction evidence

- Missing-transport `m.image` replacement: bundled validator `True`, full parser `None`, cache validator `True`.
- Empty-ID replacement: full resolver applied body `malformed` with visible event ID `""`.
- Timestamp skew: original `100000`, edit `30000`, visible activity incorrectly became `30000`.
- Cache identity mismatch: lookup key `$requested` returned payload event ID `$other`.
- Invalid cached original: point read and snapshot both returned edited text without a network fallback.
- Approval fallback: older valid status was `approved`, while the accepted malformed newest replacement restored `pending`.

## Last clean validation

- Production and test baseline `275bdf78edebb4ffaedd2b6b64255f4e6e91a09b` passed SQLite and PostgreSQL cache suites, full pytest, Tach, and all-file pre-commit.
- Full pytest reported `11298 passed` and `327 skipped`.
- Exact head `5419f282a` had clean GitHub pytest, smoke, Greptile, and required CI before review invalidation.
- All eight GitHub review threads are resolved.

## Required next steps

- Add shared event-type-aware replacement-content validation for full, bundled, SQLite, and PostgreSQL selection.
- Reject malformed approval status replacements during ordered cache candidate selection.
- Reject empty replacement event IDs before candidate recording.
- Make visible activity time the maximum of original and accepted-edit timestamps.
- Validate cached payload identity and original message content before point or snapshot projection.
- Remove only verified redundant approval sender and room guards.
- Add deterministic full-resolution plus SQLite and PostgreSQL regressions for every blocker.
- Run focused tests before each atomic commit.
- Re-run relevant backend tests, full pytest, Tach, and all-file pre-commit under resource ownership.
- Remove this file only when a new exact head is frozen.
- Launch fresh exact-head Codex and Fable reviews after every code commit sequence.
- Run real-Tuwunel only after fresh Codex and Fable approval and resource ownership.
