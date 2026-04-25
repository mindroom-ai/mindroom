# ISSUE-197 PR #714 Final Bug-Fix Plan

## Summary

PR #714 should keep the restored-sync-token optimization, but it must fail closed when the first restored-token sync cannot prove durable cache catch-up.
The implementation should stay focused on `src/mindroom/bot.py`, `src/mindroom/matrix/sync_tokens.py`, and the related focused tests.

Acceptance criteria:

- A restored-token first sync marks `pre_runtime_thread_cache_trusted` true only when all returned timeline cache tasks finish without any `BaseException`, joined-room timeline data is valid and non-empty, and no joined room has `timeline.limited is True`.
- A restored-token first sync that has a cache failure, a cache-task cancellation, a limited joined-room timeline, empty joined-room data, or invalid joined-room data does not persist that response's advanced `next_batch` token.
- Unsafe restored-token first sync outcomes clear restored-token trust for the current runtime and leave no saved token that can make a later restart trust pre-runtime thread cache.
- A first-sync `M_UNKNOWN_POS` for a restored token clears both `client.next_batch` and the persisted token file so nio can recover through a cold sync instead of looping on the bad token.
- Cold starts may still persist valid sync tokens, but they must not mark pre-runtime thread cache as trusted.
- No source files outside the sync-token and first-sync cache-trust path should change unless a focused test proves they are necessary.

## Bugs to Fix

1. First-sync cache-task failures are classified too narrowly.
`asyncio.CancelledError` is a `BaseException`, not an `Exception`, so the current `isinstance(result, Exception)` filter can treat cancelled cache writes as success.

2. Unsafe restored-token first syncs still save the advanced token.
Cache write errors, cancellations, and limited timelines currently fail closed only in memory, then `_persist_sync_token()` saves the response token anyway.
That saved token can be restored later and paired with a clean first sync, causing old thread cache rows to be trusted even though the missed replay window was skipped.

3. Rejected saved sync tokens are not cleared.
When nio returns `M_UNKNOWN_POS` on the first sync, MindRoom marks cache trust invalid but leaves `client.next_batch` and the token file in place.
Nio can then retry the same invalid token forever, and process restarts can reproduce the same failure.

4. Empty or invalid first-sync room data grants too much trust.
A restored-token first sync with `rooms.join = {}` or a malformed joined-room container currently has no limited rooms, so it can mark catch-up applied.
That is indeterminate, not proof that durable cache caught up.

## Non-Goals

- Do not add durable checkpoint metadata or rewrite the sync-token file format in this bug-fix pass.
- Do not reorder every non-first-sync token persistence behind cache writes.
- Do not relax `THREAD_CACHE_MAX_AGE_SECONDS`.
- Do not redesign thread cache invalidation, room-scoped checkpoints, or event-cache schema.
- Do not modify SaaS, frontend, Matrix message rendering, or unrelated agent orchestration code.
- Do not add broad retries or try-except wrappers that hide unsafe cache-write failures.

## Implementation Plan

1. Add an idempotent `clear_sync_token(storage_path, agent_name)` helper in `src/mindroom/matrix/sync_tokens.py`.
It should delete `sync_tokens/<agent>.token` when present and do nothing when the file is missing.

2. Make first-sync room classification explicit in `src/mindroom/bot.py`.
Use a small helper that returns whether joined-room data is valid, how many joined rooms were inspected, and which rooms have `timeline.limited is True`.
Treat non-mapping `response.rooms.join`, empty joined-room data, missing timelines, or non-boolean `limited` values as indeterminate for restored-token cache trust.

3. In `_on_sync_response`, await first-sync cache tasks and classify all `BaseException` results as unsafe.
Re-raise `KeyboardInterrupt` and `SystemExit` results.
For `asyncio.CancelledError`, re-raise if the current task is being cancelled; otherwise treat the child-task cancellation as an unsafe cache-write outcome.

4. Compute a single `safe_restored_catchup` decision for the first sync.
It is true only when this was a restored-token startup, cache tasks all succeeded, joined-room data was valid and non-empty, and no joined timeline was limited.
Only that path should call `mark_sync_catchup_applied()` and persist the advanced token.

5. For any unsafe restored-token first sync, call `mark_restored_sync_token_invalid()`, clear `client.next_batch`, clear the saved token file, skip `_persist_sync_token()` for that response, and log the specific reason.
This prevents shutdown or a later restart from resurrecting the unsafe advanced token.

6. Preserve normal token persistence for successful cold starts and later non-first sync responses.
Keep the existing comment about the remaining non-first-sync callback window, but make sure it does not claim all cache writes are durably complete before every token save.

7. In `_on_sync_error`, handle first-sync `M_UNKNOWN_POS` for restored tokens by calling `mark_restored_sync_token_invalid()`, clearing `client.next_batch`, and deleting the saved token file with `clear_sync_token()`.
The next nio sync attempt should be a cold sync using no saved `since` token.

8. Keep `_effective_thread_cache_runtime_started_at()` behavior unchanged for the happy path.
This plan fixes the unsafe inputs to the boolean trust gate rather than replacing the gate with checkpoint metadata.

## Tests

Add or update focused unit tests with these exact behaviors:

- `tests/test_matrix_sync_tokens.py::test_clear_sync_token_removes_saved_token`
- `tests/test_matrix_sync_tokens.py::test_clear_sync_token_is_idempotent`
- `tests/test_matrix_sync_tokens.py::test_unknown_pos_first_sync_clears_client_and_saved_token`
- `tests/test_threading_error.py::TestThreadingBehavior::test_first_sync_cache_task_cancelled_does_not_trust_cache`
- `tests/test_threading_error.py::TestThreadingBehavior::test_first_sync_cache_error_skips_token_persist_and_clears_saved_token`
- `tests/test_threading_error.py::TestThreadingBehavior::test_limited_first_sync_skips_token_persist_and_clears_saved_token`
- `tests/test_threading_error.py::TestThreadingBehavior::test_empty_joined_rooms_first_sync_does_not_trust_cache`
- `tests/test_threading_error.py::TestThreadingBehavior::test_invalid_joined_rooms_first_sync_does_not_trust_cache`
- Keep `tests/test_threading_error.py::TestThreadingBehavior::test_complete_first_sync_trusts_restored_thread_cache` passing for the happy path.
- Keep `tests/test_thread_history.py::TestThreadHistoryCache::test_restored_token_post_sync_reuses_pre_runtime_thread_cache` and the untrusted restart variants passing.

Run the focused verification after implementation:

```bash
uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py -k "sync_token or first_sync or restored_token or untrusted_restart" -x -n 0 --no-cov -v
git diff --check origin/main
```

Run targeted pre-commit on the touched files before the implementation commit:

```bash
uv run pre-commit run --files src/mindroom/bot.py src/mindroom/matrix/sync_tokens.py tests/test_matrix_sync_tokens.py tests/test_threading_error.py tests/test_thread_history.py
```

## Live Test

Use the local Matrix stack only after the focused unit tests pass.
The saved token path for this PR is `mindroom_data/sync_tokens/<agent>.token`, not `mindroom_data/matrix_state.yaml`.

1. Start Matrix with `just local-matrix-up` and run MindRoom against the local homeserver.
2. Happy path: create a thread with Matty, let the bot cache it, stop MindRoom, restart with the saved token, and verify the logs show restored-token catch-up and the agent still replies in the same thread.
3. Limited path: while MindRoom is stopped, send enough room events to force a limited first sync, restart, and verify the unsafe response does not leave an advanced token in `mindroom_data/sync_tokens/<agent>.token`.
4. Rejected token path: mangle `mindroom_data/sync_tokens/<agent>.token`, restart, and verify `matrix_sync_token_rejected` is logged, the token file is removed, and the bot completes a cold sync instead of looping.
5. Finish with a Matty smoke check that the agent replies in a thread after each restart scenario.

## Risks

- Clearing a saved token after unsafe catch-up can make the next sync more expensive, but that is preferable to trusting stale durable thread cache.
- Treating empty joined-room first syncs as indeterminate may skip the optimization for agents that currently have no joined rooms.
- The remaining non-first-sync token persistence window predates this PR and remains a follow-up risk.
- If focused tests prove old durable cache rows can still be trusted after an unsafe path despite token clearing, the implementation should stop and add a narrow stale-marker or checkpoint follow-up rather than broadening this patch silently.
