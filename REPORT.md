# ISSUE-197 / PR #714 Implementation Report

## Summary

Implemented `PLAN-SIMPLIFY.md` as a smaller sync-token certification boundary.
`bot.py` now restores Matrix sync continuity, delegates certification to a state machine, applies the returned decision, and only saves certified checkpoints.
Legacy plaintext sync tokens still restore nio sync continuity, but they do not make pre-runtime thread-cache rows trustworthy.
The persisted token format is now one JSON checkpoint record containing the token and `thread_cache_valid_after`.
The sidecar/hash marker path was removed from the active trust model and is only cleaned up for legacy files.
Thread-cache trust is now controlled by one explicit `thread_cache_read_boundary`.
On ambiguous cache writes, limited timelines, missing `next_batch`, cancellation, or `M_UNKNOWN_POS`, the runtime fails closed by clearing the saved token, advancing the read boundary, and resetting nio's token when required.
Refill writes are freshness-safe because replacements validate no later than `fetch_started_at`, so writes started before a boundary move cannot later look fresh.

## Files Changed

- `src/mindroom/matrix/sync_certification.py` adds the pure certification state machine and value objects.
- `src/mindroom/matrix/sync_tokens.py` stores and loads the single JSON checkpoint format while treating plaintext tokens as sync-only legacy tokens.
- `src/mindroom/bot.py` removes the patch-loop certification machinery and rewires startup, sync callbacks, `M_UNKNOWN_POS`, and shutdown through the certifier.
- `src/mindroom/bot_runtime_view.py` collapses runtime cache trust state to `thread_cache_read_boundary`.
- `src/mindroom/matrix/conversation_cache.py` and `src/mindroom/matrix/cache/thread_writes.py` expose one durable `SyncCacheWriteResult` for sync timeline writes.
- `src/mindroom/matrix/cache/thread_write_cache_ops.py` and `src/mindroom/matrix/cache/event_cache.py` use the explicit read boundary and remove guarded-write revocation machinery.
- `tests/test_sync_certification.py` covers certifier transitions.
- `tests/test_matrix_sync_tokens.py`, `tests/test_thread_history.py`, and `tests/test_threading_error.py` keep behavioral invariant coverage and drop private flag and sidecar assertions.

## Diff Size

- Versus `origin/main`, the full branch currently changes 89 files with 4,212 insertions and 9,744 deletions.
- Versus `origin/main`, Python source under `src/**/*.py` currently changes 34 files with 1,566 insertions and 3,030 deletions.
- Versus `origin/main`, `src/mindroom/bot.py` is now 137 insertions and 75 deletions, for a net 62-line increase.
- Versus the pre-simplification branch head, this refactor changes `src/mindroom/bot.py` by 88 insertions and 459 deletions, for a net 371-line reduction.
- Versus the pre-simplification branch head, touched Python source changes are 446 insertions and 632 deletions.

## Tests Run

```bash
uv run pytest tests/test_sync_certification.py tests/test_matrix_sync_tokens.py -x -n 0 --no-cov -v
```

Result: 30 passed, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_thread_history.py tests/test_threading_error.py -x -n 0 --no-cov -v
```

Result: 218 passed, 1 existing Pydantic deprecation warning.

```bash
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix --run 'uv run pytest tests/test_matrix_sync_tokens.py tests/test_sync_certification.py tests/test_thread_history.py tests/test_threading_error.py -x -n 0 --no-cov -v'
```

Result: 248 passed, 1 existing Pydantic deprecation warning.

```bash
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix --run 'uv run pytest -x -n 0 --no-cov -v'
```

Result: pytest reported 5,131 passed, 28 skipped, and 53 warnings in 438.75 seconds.
The pytest process stayed alive after printing the passing summary and was terminated afterward.

```bash
uv sync --all-extras
```

Result: passed.

```bash
git --no-pager diff --check origin/main
```

Result: passed.

```bash
uv run pre-commit run --files src/mindroom/bot.py src/mindroom/bot_runtime_view.py src/mindroom/matrix/cache/event_cache.py src/mindroom/matrix/cache/thread_write_cache_ops.py src/mindroom/matrix/cache/thread_writes.py src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/sync_tokens.py src/mindroom/matrix/sync_certification.py tests/test_matrix_sync_tokens.py tests/test_sync_certification.py tests/test_thread_history.py tests/test_threading_error.py REPORT.md
```

Result: passed.

## Caveats

- No live Matrix smoke test was run.
- The full pytest wrapper required manual termination after the passing summary because a Python child process remained alive.
- `PLAN-SIMPLIFY.md` is preserved as an untracked review artifact alongside the existing review and triage markdown files.
