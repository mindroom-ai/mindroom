# Thread edit integrity status

## Branch and pull request

- Branch: `fix/thread-edit-integrity`.
- Pull request: https://github.com/mindroom-ai/mindroom/pull/1641.
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Latest production and test commit: `43b626592cd9513221d0207b65926b3e7afcb708`.
- Never merge this pull request.
- Never amend or force-push.

## Current gate state

- Exact head `be6ee9f9a807e5c88b07db3e1c3a8a5b7d8aa49b` was rejected by two fresh native Codex reviewers.
- Follow-up commits through `fa5b0ed377259f7124baaa8b8fd7f0e8c612175f` address every verified finding from those reviews.
- The isolated Fable review of rejected head `be6ee9f9a807e5c88b07db3e1c3a8a5b7d8aa49b` remains evidence-only.
- Current GitHub CI is running on `43b626592cd9513221d0207b65926b3e7afcb708`.
- Every prior exact-head review is invalid.
- The real-Tuwunel gate has not run on this implementation.
- `RESOURCE-GATE.md` currently assigns heavy work to reconstructed nio, with exact-head real-Tuwunel next.
- Do not run full pytest, PostgreSQL fanout, all-file hooks, Docker Matrix, or real-Tuwunel until the resource gate grants the slot.

## Completed follow-up fixes

- Full and bundled history now accept every valid `m.room.message` replacement, including `msgtype` changes.
- Direct and bundled paths reject malformed message replacements and fall back to the next valid candidate.
- Bundled extraction reads only the spec-defined `unsigned.m.relations`.
- Incremental reuse rejects state and explicit wrong-room suffix rows.
- Incremental reuse uses the canonical bundled replacement candidate extractor.
- The no-op PostgreSQL schema-v3 compatibility diff was removed.
- Deterministic regressions cover direct, bundled, incremental, SQLite, and PostgreSQL behavior.

## Latest change

- The SQLite and PostgreSQL latest-edit queries previously duplicated a long Matrix validity predicate in two JSON dialects.
- SQL now owns only cache scope, joins, and canonical timestamp plus event-ID ordering.
- One backend-neutral Python validator owns sender, type, room, state, index identity, timestamp identity, relation target, `m.new_content`, and `m.room.message` renderability.
- Both backends scan the ordered cursor only until the first valid candidate, so malformed newest edits still fall back without loading a candidate list.
- PostgreSQL retains explicit bytewise `COLLATE "C"` event-ID ordering.
- SQLite uses explicit `COLLATE BINARY` event-ID ordering.

## Current validation

- Focused backend-neutral semantics and SQLite latest-edit regressions pass with `24 passed`.
- Ruff passes on the three changed production files.
- The prior focused all-message replacement tests pass with `5 passed`.
- Incremental reuse passes with `30 passed`.
- Each follow-up commit passed its commit hooks.
- Every new commit invalidates validation as a final gate.

## Required next steps

- Monitor current CI while waiting for the heavy resource slot.
- When heavy ownership returns, run relevant SQLite and PostgreSQL backend tests, the exact thirteen prior CI failures, full pytest, Tach, and all-file pre-commit.
- Update the stale PR body.
- Remove this file only when a new final head is frozen.
- Run two fresh native Codex reviews and one fresh isolated Claude Fable 5 xhigh review on the exact frozen head.
- Verify every current GitHub review comment and CI check.
- Read the live instructions completely, repin them to the exact frozen head, and run fresh isolated real-Tuwunel validation only after fresh Codex and Fable approval and resource ownership.
- If the branch moves, invalidate and restart all exact-head gates.
