# Thread edit integrity status

## Branch and pull request

- Branch: `fix/thread-edit-integrity`.
- Pull request: https://github.com/mindroom-ai/mindroom/pull/1641.
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Last frozen local, remote, and PR head: `be6ee9f9a807e5c88b07db3e1c3a8a5b7d8aa49b`.
- Never merge this pull request.
- Never amend or force-push.

## Current gate state

- Exact head `be6ee9f9a807e5c88b07db3e1c3a8a5b7d8aa49b` is rejected.
- Both fresh native Codex reviewers returned `CHANGES REQUIRED`.
- The isolated Claude Fable 5 xhigh review is still running on the rejected head for evidence only.
- Any CI or AI approval on the rejected head is non-gating.
- The real-Tuwunel gate has not run on this implementation.
- `RESOURCE-GATE.md` currently assigns heavy work to PR #1640, with reconstructed nio ahead of exact-head real-Tuwunel.
- Do not run full pytest, PostgreSQL fanout, all-file hooks, Docker Matrix, or real-Tuwunel until the resource gate grants the slot.

## Verified blockers

- Full and bundled thread resolution record only `RoomMessageText` and `RoomMessageNotice` replacements.
- Matrix permits a replacement `m.room.message` to change `msgtype`, including `m.text` to `m.emote`.
- Full resolution accepts a replacement whose `m.new_content` has `body` but no required `msgtype`, while bundled and SQLite/PostgreSQL paths reject it.
- Incremental reuse does not reject explicit wrong-room or state suffix rows before merging.
- Bundled extraction accepts non-spec top-level `m.relations`, while incremental reuse inspects only the spec-defined `unsigned.m.relations`, causing full/reuse divergence.
- PostgreSQL schema-v3 compatibility changes in the diff are semantic no-ops and should be restored to `origin/main`.

## Planned fixes

- Record every valid nio `RoomMessage` replacement while keeping originals visible across all supported message `msgtype` values.
- Share in-memory replacement renderability validation so direct and bundled paths require valid `m.room.message` content.
- Read bundled aggregations only from `unsigned.m.relations`.
- Pass authoritative room scope into the incremental suffix guard and reject state or explicit wrong-room rows.
- Reuse the canonical bundled candidate extractor in the suffix guard.
- Restore the no-op PostgreSQL schema compatibility expressions to `origin/main`.
- Add deterministic full/bundled and incremental-reuse regressions.

## Existing validation

- The prior owning/backend matrix passed at 100%.
- The thirteen published pytest regressions pass in a focused `47 passed` rerun.
- Isolated SQLite/PostgreSQL fanout, seeded trace, and knowledge-status tests passed with `5 passed`.
- Prior exact-head GitHub pytest passed with `11590 passed, 14 skipped`.
- Tach and all-file pre-commit passed before this reopened handoff commit.
- Every new commit invalidates that validation as a final gate.

## Required final gates

- Run focused tests after each fix commit.
- When heavy ownership returns, run relevant SQLite/PostgreSQL backend tests, full pytest, Tach, and all-file pre-commit.
- Remove this file only when a new final head is frozen.
- Run two fresh native Codex reviews and one fresh isolated Claude Fable 5 xhigh review on the exact frozen head.
- Verify every current GitHub review comment and CI check.
- Read the live instructions completely, repin them to the exact frozen head, and run fresh isolated real-Tuwunel validation only after fresh Codex and Fable approval and resource ownership.
- If the branch moves, invalidate and restart all exact-head gates.
