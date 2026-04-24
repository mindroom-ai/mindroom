# ISSUE-197 / PR #714 Implementation Report

## Implementation

- Added `clear_sync_token(storage_path, agent_name)` and used it to remove bad saved Matrix sync tokens.
- Replaced the first-sync limited-room-only check with explicit joined-room classification.
- Treated empty joined-room data, non-mapping joined-room data, missing timelines, and non-boolean `timeline.limited` values as unsafe for restored-token cache trust.
- Treated all first-sync cache task `BaseException` results as unsafe, including `asyncio.CancelledError`.
- Preserved `KeyboardInterrupt`, `SystemExit`, and active parent-task cancellation propagation.
- Skipped token persistence after first-sync cache write failures or cancellations.
- For unsafe restored-token first syncs, cleared runtime trust, `client.next_batch`, and the saved token file.
- For first-sync `M_UNKNOWN_POS`, cleared runtime trust, `client.next_batch`, and the saved token file so nio can recover with a cold sync.
- Round 1 fix: restored-token first-sync catch-up now asks the real sync timeline writer to re-raise durable cache write failures instead of only logging them.
- Round 1 fix: strict first-sync cache writes cover sync event stores, thread appends, incremental revalidation, stale marker writes, fail-closed deletes, and redaction invalidation paths.
- Round 1 fix: after an unsafe restored-token first sync or restored-token `M_UNKNOWN_POS`, same-runtime sync-token persistence is suppressed so a later successful sync cannot become a stale-cache trust root.

## Tests Run

```bash
uv run python -m py_compile src/mindroom/bot.py src/mindroom/bot_runtime_view.py src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/thread_write_cache_ops.py src/mindroom/matrix/cache/thread_writes.py tests/test_matrix_sync_tokens.py tests/test_threading_error.py
```

```bash
env NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos nix-shell --run 'uv run pytest tests/test_matrix_sync_tokens.py::test_unknown_pos_restored_first_sync_suppresses_later_token_persistence tests/test_threading_error.py::TestThreadingBehavior::test_restored_first_sync_real_store_failure_fails_closed tests/test_threading_error.py::TestThreadingBehavior::test_restored_first_sync_real_revalidation_failure_fails_closed tests/test_threading_error.py::TestThreadingBehavior::test_restored_first_sync_real_stale_marker_failure_fails_closed tests/test_threading_error.py::TestThreadingBehavior::test_unsafe_restored_first_sync_suppresses_later_saved_token_for_restart -x -n 0 --no-cov -v'
```

Result: 5 passed, 1 existing Pydantic deprecation warning.

```bash
env NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos nix-shell --run 'uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -k "sync_token or first_sync or restored_token or untrusted_restart" -x -n 0 --no-cov -v'
```

Result: 24 passed, 208 deselected, 1 existing Pydantic deprecation warning.

```bash
git --no-pager diff --check origin/main
```

Result: passed.

```bash
uv sync --all-extras
uv run pre-commit run --files src/mindroom/bot.py src/mindroom/bot_runtime_view.py src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/thread_write_cache_ops.py src/mindroom/matrix/cache/thread_writes.py tests/test_matrix_sync_tokens.py tests/test_threading_error.py REPORT.md
```

Result: passed.

## Deviations

- No live test was run, per the instruction that live testing is Phase 4 after review approval.
- Scope stayed within the two forwarded round 1 blockers.
