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
- Round 2 fix: restored-token first-sync catch-up now requires the durable event cache and write coordinator to be available before trusting pre-runtime thread cache.
- Round 2 fix: disabled or uninitialized event caches no longer produce successful no-op sync catch-up for restored tokens.
- Round 2 fix: joined-room first-sync classification now rejects malformed non-list `timeline.events` data before restored-token trust can be applied.
- Round 3 fix: all sync-token persistence now waits for sync timeline cache writes to complete successfully before saving the corresponding `next_batch`.
- Round 3 fix: shutdown and hot-reload token flushes are blocked while sync-cache catch-up is still pending.
- Round 3 fix: failed, cancelled, unavailable, limited, or malformed sync timeline catch-up suppresses same-runtime token persistence so later tokens cannot become stale-cache trust roots.
- Round 3 fix: malformed entries inside `timeline.events` fail closed before the cache writer can raise and before any token persistence path can run.
- Round 4 fix: shutdown and hot-reload now flush only the latest in-memory cache-certified sync token, not a raw `client.next_batch` candidate that nio advanced before the response callback started certification.
- Round 4 fix: strict sync cache catch-up now rechecks durable event-cache availability after queued cache writes drain, so availability lost after preflight fails closed instead of certifying no-op writes.
- Round 4 fix: post-start `M_UNKNOWN_POS` now clears `client.next_batch`, the saved token file, and restored-token trust state through the same localized bad-token handler.
- Round 5 fix: certified sync tokens now carry a hash-backed provenance marker, while legacy plaintext tokens restore only for Matrix continuity and suppress same-runtime token persistence.
- Round 5 fix: every `M_UNKNOWN_POS` now poisons later token persistence, including non-restored runtimes.
- Round 5 fix: later sync cache certification failures revoke current in-memory pre-runtime thread-cache trust before strict thread reads can reuse old rows.
- Round 6 fix: certified sync-token markers now persist `thread_cache_valid_after` with the token digest, while marker-only and plaintext tokens remain cache-trust fail-closed.
- Round 6 fix: restored-token thread-cache reuse now uses the persisted valid-after boundary instead of removing the restart freshness boundary.
- Round 6 fix: tokens certified by cold runtimes use that runtime start as the cache boundary, and later restored-token certifications carry the boundary forward.
- Round 7 fix: disabled or unavailable durable event caches now block sync-token certification even for empty joined-room timelines.
- Round 7 fix: once durable cache availability fails, the runtime suppresses later same-runtime token persistence until a fresh safe runtime state is established.
- Round 8 fix: uncertified strict sync-cache certification now advances the live thread-cache freshness boundary before reporting the failure, forcing current-runtime thread rows validated before the failure to refetch.
- Round 8 fix: parent cancellation during restored first-sync certification now clears restored-token trust, `client.next_batch`, and the saved token before re-raising, so a same-bot sync-loop restart cannot certify a skipped batch.
- Round 9 fix: parent cancellation during any sync-cache certification now revokes current-runtime thread-cache trust before re-raising and rewinds `client.next_batch` to the last cache-certified token when available.
- Round 9 fix: runtime trust revocation now installs a guarded thread-write boundary, so prewarm fetches that started before revocation cannot later write snapshots that look fresh.

## Tests Run

```bash
uv run python -m py_compile src/mindroom/bot.py src/mindroom/bot_runtime_view.py tests/test_threading_error.py
```

Result: passed.

```bash
uv run pytest tests/test_threading_error.py::TestThreadingBehavior::test_non_first_sync_waits_for_cache_write_before_token_persist tests/test_threading_error.py::TestThreadingBehavior::test_restored_first_sync_shutdown_does_not_flush_pending_uncertified_token tests/test_threading_error.py::TestThreadingBehavior::test_non_first_sync_cache_failure_suppresses_current_and_later_token_persistence tests/test_threading_error.py::TestThreadingBehavior::test_non_first_sync_unavailable_event_cache_suppresses_token_for_event_timeline tests/test_threading_error.py::TestThreadingBehavior::test_malformed_timeline_event_entry_first_sync_does_not_trust_cache -x -n 0 --no-cov -v
```

Result: 5 passed, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -k "sync_token or first_sync or restored_token or untrusted_restart" -x -n 0 --no-cov -v
```

Result: 31 passed, 208 deselected, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -x -n 0 --no-cov -v
```

Result: 239 passed, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py::test_prepare_for_sync_shutdown_skips_precallback_uncertified_token tests/test_matrix_sync_tokens.py::test_unknown_pos_after_first_sync_clears_client_and_saved_token tests/test_threading_error.py::TestThreadingBehavior::test_restored_first_sync_cache_disabled_before_queued_write_runs -x -n 0 --no-cov -v
```

Result: 3 passed, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -k "sync_token or first_sync or restored_token or untrusted_restart" -x -n 0 --no-cov -v
```

Result: 34 passed, 208 deselected, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -x -n 0 --no-cov -v
```

Result: 242 passed, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py::test_legacy_plaintext_sync_token_restores_without_cache_trust tests/test_matrix_sync_tokens.py::test_unknown_pos_non_restored_runtime_suppresses_later_token_persistence tests/test_threading_error.py::TestThreadingBehavior::test_non_first_sync_cache_failure_revokes_restored_thread_cache_trust -x -n 0 --no-cov -v
```

Result: 3 passed, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -k "sync_token or first_sync or restored_token or untrusted_restart" -x -n 0 --no-cov -v
```

Result: 37 passed, 208 deselected, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -x -n 0 --no-cov -v
```

Result: 245 passed, 1 existing Pydantic deprecation warning.

```bash
uv run python -m py_compile src/mindroom/matrix/sync_tokens.py src/mindroom/bot_runtime_view.py src/mindroom/bot.py tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py
```

Result: passed.

```bash
uv run pytest tests/test_threading_error.py::TestThreadingBehavior::test_restored_cold_certified_token_rejects_cache_before_valid_after tests/test_thread_history.py::TestThreadHistoryCache::test_restored_token_post_sync_reuses_pre_runtime_thread_cache tests/test_matrix_sync_tokens.py::test_marker_without_thread_cache_boundary_is_not_certified tests/test_matrix_sync_tokens.py::test_on_sync_response_persists_latest_sync_token -x -n 0 --no-cov -v
```

Result: 4 passed, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -k "sync_token or first_sync or restored_token or untrusted_restart or valid_after" -x -n 0 --no-cov -v
```

Result: 39 passed, 208 deselected, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -x -n 0 --no-cov -v
```

Result: 247 passed, 1 existing Pydantic deprecation warning.

```bash
git --no-pager diff --check origin/main
```

Result: passed.

```bash
uv sync --all-extras
uv run pre-commit run --files src/mindroom/bot.py src/mindroom/bot_runtime_view.py src/mindroom/matrix/sync_tokens.py src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/thread_write_cache_ops.py tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py REPORT.md
```

Result: passed.

```bash
uv run pytest tests/test_threading_error.py::TestThreadingBehavior::test_non_first_sync_disabled_event_cache_suppresses_empty_token_and_later_persistence tests/test_threading_error.py::TestThreadingBehavior::test_non_first_sync_unavailable_event_cache_suppresses_token_for_event_timeline tests/test_matrix_sync_tokens.py::test_on_sync_response_persists_latest_sync_token -x -n 0 --no-cov -v
```

Result: 3 passed, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -x -n 0 --no-cov -v
```

Result: 248 passed, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_threading_error.py -k "store_failure_revokes_current_runtime_thread_cache or cancelled_restored_first_sync_clears_advanced_token_before_restart" -x -n 0 --no-cov -v
```

Result: 2 passed, 172 deselected, 1 existing Pydantic deprecation warning.

```bash
uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -x -n 0 --no-cov -v
```

Result: 250 passed, 1 existing Pydantic deprecation warning.

```bash
uv sync --all-extras
git --no-pager diff --check
uv run pre-commit run --files src/mindroom/bot.py src/mindroom/bot_runtime_view.py tests/test_threading_error.py REPORT.md
```

Result: passed.

```bash
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix --run 'uv run pytest tests/test_threading_error.py -k "cancelled_non_first_sync_revokes_current_thread_cache_before_restart or prewarm_started_before_trust_revocation_does_not_seed_fresh_cache" -x -n 0 --no-cov -v'
```

Result: 2 passed, 174 deselected, 1 existing Pydantic deprecation warning.

```bash
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix --run 'uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -x -n 0 --no-cov -v'
```

Result: 252 passed, 1 existing Pydantic deprecation warning.

```bash
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix --run 'uv run pytest tests/test_event_cache.py tests/test_bot_ready_hook.py -x -n 0 --no-cov -v'
```

Result: 64 passed, 1 existing Pydantic deprecation warning.

```bash
git --no-pager diff --check
```

Result: passed.

```bash
uv sync --all-extras
uv run pre-commit run --files src/mindroom/bot.py src/mindroom/bot_runtime_view.py src/mindroom/matrix/cache/event_cache.py tests/test_threading_error.py
```

Result: passed.

```bash
nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix --run 'uv run pytest -x -n 0 --no-cov -q'
```

Result: pytest summary reported 5135 passed, 28 skipped, 49 warnings in 490.53s.
The wrapper process stayed alive after printing the passing summary and was terminated afterward.

## Deviations

- No live test was run, per the instruction that live testing is Phase 4 after review approval.
- Scope stayed within the forwarded restored-token trust blockers.
