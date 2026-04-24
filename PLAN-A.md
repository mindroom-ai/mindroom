# ISSUE-197 plan from PR #714

## Scope

This plan is based on `git diff origin/main...origin/pr-714` after fetching `refs/pull/714/head`.
The PR under review is `https://github.com/mindroom-ai/mindroom/pull/714`.
No source implementation is included in this worktree plan.

## PR intent summary

PR #714 tries to remove restart-time thread-history slowness by reusing durable thread caches after Matrix sync catch-up proves the local cache has seen downtime events.
It restores a saved Matrix `next_batch` token, waits for first-sync timeline cache writes, and then lets thread-history reads pass `runtime_started_at=None` when `BotRuntimeState.pre_runtime_thread_cache_trusted` is true.
It fails closed for the current runtime when the first sync has a limited joined-room timeline or when a restored token is rejected with `M_UNKNOWN_POS`.
It keeps cold starts and pre-catch-up reads on the old `runtime_started_at` freshness boundary.

## Bug findings

### 1. The PR erases the freshness boundary too broadly after one successful restored-token catch-up.

Evidence: `origin/pr-714:src/mindroom/matrix/conversation_cache.py:411-415` returns `None` from `_effective_thread_cache_runtime_started_at()` whenever `pre_runtime_thread_cache_trusted` is true.
Evidence: `origin/pr-714:src/mindroom/matrix/cache/thread_cache_helpers.py:46-47` only rejects `validated_before_runtime_start` when `runtime_started_at is not None`.
This means one successful first sync makes every durable thread snapshot eligible, including snapshots validated before a prior cold start, before a prior limited catch-up, or before a prior cache-write failure.
The trust should be bounded to a durable checkpoint, not represented by removing the boundary entirely.

### 2. Unsafe catch-up outcomes still persist the advanced sync token.

Evidence: `origin/pr-714:src/mindroom/bot.py:1001-1017` logs first-sync cache write errors or marks the restored token invalid for limited timelines, but `origin/pr-714:src/mindroom/bot.py:1023` still calls `_persist_sync_token()` unconditionally.
If the first restored-token sync is limited, the current runtime rejects pre-runtime caches, but the advanced token is saved.
On a later restart that token can have a non-limited first sync, `pre_runtime_thread_cache_trusted` becomes true, and old thread snapshots can be accepted even though the only Matrix replay window that could have filled the omitted events was already skipped.
The same problem exists after first-sync cache write failures because the PR logs `matrix_sync_cache_catchup_failed` but does not durably invalidate affected thread caches or prevent later token trust.

### 3. Normal sync responses still save tokens before cache writes are durable.

Evidence: `origin/pr-714:src/mindroom/bot.py:996-1000` awaits cache write tasks only for `first_sync_response`.
Evidence: `origin/pr-714:src/mindroom/bot.py:1019-1023` then persists `next_batch` even though non-first sync timeline cache writes are still background tasks.
A crash after `_persist_sync_token()` but before a queued `matrix_cache_sync_timeline` write finishes can leave the saved token ahead of SQLite thread-cache state.
The next restart can restore that token, see a clean first sync, and trust a thread cache that is missing the event skipped by the persisted token.

### 4. Rejected saved tokens are not cleared, so recovery can loop on the same invalid token.

Evidence: `origin/pr-714:src/mindroom/bot.py:1037-1043` handles first-sync `M_UNKNOWN_POS` by only clearing runtime cache trust state and logging.
Matrix-nio `AsyncClient.sync()` uses `since or self.next_batch`, and a `SyncError` does not clear `client.next_batch`.
After the response callback, `sync_forever()` continues and future sync calls can keep sending the same invalid token.
The saved token file also remains, so process restarts can repeat the same failure.

### 5. First-sync cache task cancellation can be mistaken for success.

Evidence: `origin/pr-714:src/mindroom/bot.py:998-1001` types gather results as `object | BaseException` but counts only `isinstance(result, Exception)`.
`asyncio.CancelledError` is a `BaseException` on supported Python versions, so a cancelled cache task can be ignored and the catch-up can be marked applied.
This is less likely than the token/checkpoint bugs, but it is a real fail-open path in the new trust gate.

## Files likely affected

- `src/mindroom/bot.py`
- `src/mindroom/bot_runtime_view.py`
- `src/mindroom/matrix/sync_tokens.py`
- `src/mindroom/matrix/conversation_cache.py`
- `src/mindroom/matrix/cache/thread_writes.py`
- `src/mindroom/matrix/cache/thread_write_cache_ops.py`
- `src/mindroom/matrix/cache/thread_cache_helpers.py`
- `src/mindroom/matrix/cache/event_cache.py`
- `src/mindroom/matrix/cache/event_cache_threads.py`
- `tests/test_threading_error.py`
- `tests/test_thread_history.py`
- `tests/test_event_cache.py`
- `tests/test_thread_mode.py`

## Proposed fix approach

### A. Replace the boolean trust gate with a persisted thread-cache checkpoint.

Do not infer thread-cache trust from the mere existence of a saved sync token.
Store sync-token metadata with a `thread_cache_valid_after` or equivalent checkpoint timestamp.
On startup, restore the token for Matrix continuity, but only relax the current runtime boundary after the first sync catch-up succeeds.
After a safe catch-up, use the persisted checkpoint as the effective `runtime_started_at` for thread-cache reads instead of passing `None`.
For old plaintext token files or token files without checkpoint metadata, treat the token as usable for sync but not as proof that old thread caches are reusable.

### B. Maintain a durable invariant before saving any sync token.

The invariant should be: a persisted sync token must never advance beyond Matrix timeline events that are neither stored in the thread/event cache nor covered by a durable freshness boundary.
Await or otherwise chain `matrix_cache_sync_timeline` work before saving the corresponding token.
Apply this rule to every sync response, not only the first sync after startup.
If awaiting every write inline is too expensive, persist the token in a continuation that is ordered after the room cache tasks, but do not let a later token persistence overtake an earlier unfinished cache write.

### C. Treat unsafe sync results as checkpoint bumps, not just in-memory trust failures.

For a limited first sync, a cache write failure, or a cache write cancellation, keep the effective cache boundary at the current runtime start or bump it to the failure time.
Persist future tokens with that conservative boundary so a later restart cannot resurrect older snapshots.
If a room-specific stale marker approach is preferred, make the cache task return room metadata and durably mark all limited or failed rooms stale before persisting the token.
Do not mark catch-up applied until all cache writes and all required stale markers have completed without `BaseException`.

### D. Clear rejected sync tokens.

Add a small `clear_sync_token(storage_path, agent_name)` helper in `src/mindroom/matrix/sync_tokens.py`.
When first sync returns `M_UNKNOWN_POS` for a restored token, set `client.next_batch = None`, delete the saved token, and keep the thread-cache boundary at the current runtime start.
The next sync should be a cold sync, and future persisted tokens should carry a conservative checkpoint so old caches stay rejected.

### E. Decide whether the 300 second age guard still applies to checkpoint-covered thread caches.

`THREAD_CACHE_MAX_AGE_SECONDS` can still force homeserver refetches even after a valid sync-token catch-up.
If ISSUE-197 is specifically about restart slowness, add an explicit "covered by sync checkpoint" mode that skips the wall-clock age rejection for thread history while preserving invalidation and redaction checks.
Keep the age guard for unrelated agent-message snapshot reads unless tests prove the same checkpoint semantics are valid there.

## Test strategy

Add a unit test where a durable cache row predates a prior cold or unsafe runtime, a later token is restored, and the first sync is non-limited.
Assert the stale pre-checkpoint row is rejected and the homeserver path is used.
Add a unit test where a non-first sync cache write is delayed, `_on_sync_response` runs, and the token is not persisted until the write completes.
Add a unit test where a non-first sync cache write fails and later token persistence keeps or bumps the conservative checkpoint.
Add a unit test where first restored-token sync is limited, the next token metadata cannot make pre-limited cache rows trusted on a later restart.
Add a unit test for `M_UNKNOWN_POS` that asserts `client.next_batch` is cleared and the saved token file is removed.
Add a unit test where first-sync cache gather returns `asyncio.CancelledError`, and assert catch-up is not marked trusted.
Update existing PR tests in `tests/test_thread_history.py` so restored-token reuse expects a checkpoint timestamp rather than a `None` boundary.
Run the relevant focused suite with the NixOS wrapper:

```bash
export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos
nix-shell --run 'uv run pytest tests/test_thread_history.py tests/test_threading_error.py tests/test_event_cache.py tests/test_thread_mode.py -x -n 0 --no-cov -v'
```

Then run the broader backend suite before merge:

```bash
export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos
nix-shell --run 'uv run pytest -x -n 0 --no-cov -v'
```

Run pre-commit only after `uv sync --all-extras`:

```bash
uv sync --all-extras
uv run pre-commit run --all-files
```

## Live-test strategy

Use the local Matrix stack because the bug depends on real sync-token and `/sync` behavior.
Start Synapse with `just local-matrix-up`.
Run MindRoom against the local homeserver and local OpenAI-compatible endpoint as documented in `AGENTS.md`.
Create a thread, let the bot cache the thread, stop the backend, then restart with the saved sync token and verify the thread history comes from cache only when the checkpoint should allow it.
Repeat with a forced invalid token by editing or deleting the saved token file and verify the next sync cold-starts instead of looping on `M_UNKNOWN_POS`.
Simulate a limited catch-up by creating more downtime events than the sync timeline limit or by using a test homeserver filter with a small timeline limit, then verify old cache rows are not trusted after another restart.
Use Matty to verify the agent still replies in the correct thread after restart:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false uv run --python 3.13 matty send "Lobby" "Hello @mindroom_general:localhost please reply with pong."
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false uv run --python 3.13 matty threads "Lobby"
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false uv run --python 3.13 matty thread "Lobby" t1
```

## Risks

Awaiting cache writes before token persistence can add sync-loop latency if SQLite is slow or a room write queue is backed up.
A continuation-based token writer avoids blocking more of the sync loop, but it must preserve ordering across sync responses.
Changing `sync_tokens.py` from plaintext to structured metadata can affect existing local installs, but the repository guidance allows breaking stale local state if the new behavior is simpler and correct.
Using a global checkpoint is conservative and may refetch more than a room-scoped checkpoint after one bad room.
Room-scoped checkpoints are more precise but add more state and more ways to get the trust calculation wrong.
Relaxing the 300 second age guard can improve ISSUE-197 performance, but it should be limited to caches covered by a successful sync-token checkpoint.

## What not to change

Do not implement a production fallback that silently trusts old plaintext token files.
Do not remove invalidation, redaction, or room-stale checks from `thread_cache_rejection_reason`.
Do not weaken point lookup freshness for `get_event()` or MXC text cache reads.
Do not change Matrix event callback threading semantics unless a test proves token persistence cannot be ordered safely otherwise.
Do not add broad retries or try-except wrappers around cache writes that hide unsafe persistence.
Do not modify unrelated SaaS, frontend, or agent orchestration code for this issue.

## Verification performed for this plan

Fetched `origin/main` and `refs/pull/714/head` into `origin/pr-714`.
Inspected the three-dot diff with `git diff origin/main...origin/pr-714`.
Read the changed cache, runtime, and sync-token code paths.
Ran `git diff --check origin/main...origin/pr-714`.
