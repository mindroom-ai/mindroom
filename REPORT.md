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

## Tests Run

```bash
uv run python -m py_compile src/mindroom/bot.py src/mindroom/matrix/sync_tokens.py tests/test_matrix_sync_tokens.py tests/test_threading_error.py
```

```bash
env NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos nix-shell --run 'uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -k "sync_token or first_sync or restored_token or untrusted_restart" -x -n 0 --no-cov -v'
```

Result: 19 passed, 208 deselected, 1 existing Pydantic deprecation warning.

```bash
git --no-pager diff --check origin/main
```

Result: passed.

```bash
uv sync --all-extras
uv run pre-commit run --files src/mindroom/bot.py src/mindroom/matrix/sync_tokens.py tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py
```

Result: passed.

## Deviations

- No live test was run, per the instruction that live testing is Phase 4 after review approval.
- Scope stayed within `PLAN.md`: `src/mindroom/bot.py`, `src/mindroom/matrix/sync_tokens.py`, and focused unit tests.
