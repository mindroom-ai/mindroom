# Thread edit integrity status

## Branch and pull request

- Branch: `fix/thread-edit-integrity`.
- Pull request: https://github.com/mindroom-ai/mindroom/pull/1641.
- Base: `origin/main` at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Latest production and test commit: `275bdf78edebb4ffaedd2b6b64255f4e6e91a09b`.
- Never merge this pull request.
- Never amend or force-push.

## Current gate state

- Exact head `be6ee9f9a807e5c88b07db3e1c3a8a5b7d8aa49b` was rejected by two fresh native Codex reviewers.
- Follow-up commits through `e728373f7136c0e5deb2a3ceda6fac179cf869cb` address every verified finding from those reviews and the rejected-head Fable review.
- Test-only successor `275bdf78edebb4ffaedd2b6b64255f4e6e91a09b` corrects one stale invalid stream-edit fixture exposed by full pytest.
- The isolated Fable review of rejected head `be6ee9f9a807e5c88b07db3e1c3a8a5b7d8aa49b` returned `CHANGES REQUIRED` and is evidence-only.
- Current GitHub CI targets `275bdf78edebb4ffaedd2b6b64255f4e6e91a09b`.
- Every prior exact-head review is invalid.
- The real-Tuwunel gate has not run on this implementation.
- PR #1641 released the heavy resource slot after PostgreSQL coverage, full pytest, Tach, and all-file pre-commit passed.
- Nio PR #20 exact-head real-Tuwunel is first in the resource queue.

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
- Visible-message ordering retains the original timestamp while stale-stream recovery separately tracks the accepted edit timestamp as activity time.
- The rejected-head Fable review's no-op schema and loop-invariant findings are removed.
- Its bundled-candidate export finding is obsolete because incremental reuse consumes that API.
- The completed stale Fable process is stopped while its report remains preserved.

## Current validation

- Focused backend-neutral semantics and SQLite latest-edit regressions pass with `24 passed`.
- The exact prior CI failure files pass with `47 passed`.
- `tests/test_event_cache.py` passes with `148 passed` across SQLite and PostgreSQL.
- `tests/test_event_cache_backends.py` passes with `45 passed`, including hostile ICU collation and schema-v3 index coverage.
- Stale-stream activity-window regressions pass with `4 passed`.
- Thread-resolution reuse and neighboring activity consumers pass with `34 passed`.
- Room-scan minimality regressions pass with `3 passed`.
- Exact-head full pytest passes with `11298 passed` and `327 skipped`.
- Explicit Tach passes.
- All-file pre-commit passes after excluding the known seven unrelated frontend Prettier rewrites.
- Each follow-up commit passed its commit hooks.
- Every new commit invalidates validation as a final gate.

## Required next steps

- Update the PR body with current exact validation.
- Remove this file only when a new final head is frozen.
- Run two fresh native Codex reviews and one fresh isolated Claude Fable 5 xhigh review on the exact frozen head.
- Verify every current GitHub review comment and CI check.
- Read the live instructions completely, repin them to the exact frozen head, and run fresh isolated real-Tuwunel validation only after fresh Codex and Fable approval and resource ownership.
- If the branch moves, invalidate and restart all exact-head gates.
