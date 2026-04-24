# PLAN-B Critique of PLAN-A (PR #714 / ISSUE-197)

Reviewer: Planning Agent B.
Inputs read: `PLAN-A.md`, `PLAN-B.md`, plus PR diff already inspected for PLAN-B.

## 1. PLAN-A findings I agree are real bugs

### A-2 — Unsafe catch-up outcomes still persist the advanced sync token. **Agree, must-fix.**

PLAN-A is right that `_persist_sync_token()` runs unconditionally after the first-sync gate. My PLAN-B flagged the cache-error half of this (B-2), but PLAN-A correctly extends it to the **limited-timeline** branch as well: the in-memory `mark_restored_sync_token_invalid()` only protects the *current* runtime. The advanced `next_batch` is still written to disk, so the **next** restart can pair that token with a clean first sync, flip `pre_runtime_thread_cache_trusted` to True, and trust thread rows that were never hydrated for the events skipped by the saved token. This is the load-bearing bug behind the whole "trust pre-runtime cache" mechanism. My B-2 fix direction must be widened to cover *both* unsafe branches.

### A-4 — Rejected saved tokens are not cleared, recovery loops. **Agree, must-fix.** (Same as my B-3.)

PLAN-A's evidence chain (`client.next_batch` set only in `_restore_saved_sync_token` / nio's `_handle_sync`, never cleared; `sync_forever` re-uses `self.next_batch`; no recovery in MindRoom; saved file persists across restarts) matches what I found independently. PLAN-A also correctly identifies that the saved file must be deleted, not just `client.next_batch` cleared — I had only proposed the in-memory reset. PLAN-A's `clear_sync_token(...)` helper is the right shape.

### A-5 — `CancelledError` slips past `isinstance(result, Exception)`. **Agree, must-fix.** (Same as my B-1.)

Same root cause we both reached: gather returns the `CancelledError` instance, the `Exception` filter drops it, and the code proceeds to `mark_sync_catchup_applied()`. Switch to `BaseException`, re-raise if the parent task is itself being cancelled, otherwise treat as fail-closed (do **not** mark catch-up applied, and `mark_restored_sync_token_invalid()` for symmetry with the limited branch).

## 2. PLAN-A findings I think are partly theoretical / scope-creep

### A-1 — Replace boolean trust with a persisted thread-cache checkpoint. **Partly agree; over-built for this PR.**

The concern is real (an in-memory boolean cannot represent the full provenance of every durable cache row), but the proposed remedy — persisting checkpoint metadata alongside the token, and using that as an effective `runtime_started_at` substitute — is a bigger architectural change than ISSUE-197 needs. **If we close the loop on A-2/A-3 (no token is ever persisted past unfilled cache state), then by induction every restored token implies the durable cache is at least as advanced as the token, and the boolean flag is sufficient.** The "old snapshots from a prior cold start could be trusted" failure mode that A-1 fears can only happen because A-2/A-3 let an unsafe token escape to disk in the first place. Fix the persistence invariant; don't add a parallel checkpoint store.

If we still want defense-in-depth, the cheapest variant is to bump (or clear) the saved token's mtime / sidecar marker on every unsafe outcome — not a full structured-token rewrite.

### A-3 — Non-first sync responses persist tokens before background cache writes. **Real, but pre-existing and out of scope.**

This race exists on `origin/main` too — PR #714 did not introduce it, and the gather/await it added is intentionally limited to the first sync. Awaiting every per-batch cache task in `_on_sync_response` would add per-sync-loop latency proportional to SQLite write time, which is exactly what ISSUE-197 is trying to *reduce*. The PR's new trust gate makes the *consequences* of this race worse, but the right place to fix it is a follow-up ordering refactor (continuation-style token writer, or a cache-write barrier per room), not bundled into this PR. Note it in the PR description, file a follow-up issue, do not gate ISSUE-197 on it.

### A-1 secondary: Relaxing `THREAD_CACHE_MAX_AGE_SECONDS` (PLAN-A's section E). **Out of scope.**

Worth considering once the trust invariant is correct, but it changes a separate freshness knob and risks regressing unrelated read paths. Defer.

## 3. PLAN-A findings my PLAN-B missed

- **The full disk-side blast radius of A-2.** I caught the cache-error case in B-2 but missed that the **limited-timeline** branch has the same disk-persistence bug. My fix direction needs to be broadened: any unsafe outcome (cache error, limited timeline, cancellation) must skip `_persist_sync_token()` *and* clear or roll back `client.next_batch` so the next iteration doesn't simply re-save the same advanced token.
- **The `clear_sync_token(...)` helper.** My B-3 only proposed clearing `client.next_batch` in memory; PLAN-A correctly adds *delete the saved file* so a subsequent process restart cannot resurrect the rejected token.
- **The framing of A-1 as a provenance problem.** Even if I disagree with the heavyweight fix, PLAN-A's framing ("the boolean encodes one event in time, but the cache rows it tries to attest to span many prior runtimes") is a useful invariant to keep in mind when writing the patch. It explains why the persistence invariant in A-2 is the load-bearing one.

## 4. Bugs my PLAN-B found that PLAN-A missed

- **B-4 — Empty-join first sync silently grants trust.** Real but minor. A first sync with no joined rooms (cold provisioning, or an agent kicked from every room during downtime) trivially passes the limited-timeline check and flips `mark_sync_catchup_applied()`. Cheap fix: require `len(joined_rooms) >= 1` before trusting catch-up.
- **B-6 — `_first_sync_done = True` set on the cache-error path.** Once flipped, the new pre-first-sync M_UNKNOWN_POS branch in `_on_sync_error` no longer fires. Combined with A-4 / B-3 this drains diagnostic signal. Worth tightening when fixing A-2.
- **B-5 (`isinstance(..., dict)` fail-open) and B-7 (cosmetic log gating).** Defensive / cosmetic. **Drop both** under the "small scope" rule unless they're a one-line drive-by.

## 5. Recommended Final Implementation Scope for ISSUE-197

Order matters; each item is independently shippable but the PR should land them together.

1. **Fix the gather error filter (A-5 / B-1).**
   - `isinstance(result, BaseException)` instead of `Exception`.
   - On `CancelledError`: re-raise if the current task is being cancelled; otherwise treat as a failure (do **not** call `mark_sync_catchup_applied`, and call `mark_restored_sync_token_invalid()` for symmetry).

2. **Make the first-sync persistence honour the cache invariant (A-2 + B-2 + B-6).**
   - Compute `safe_to_advance = (no cache errors) and (no limited joined-room timeline) and (no cancellation)`.
   - On `not safe_to_advance` during the first sync after a restored token: skip `_persist_sync_token()`, **roll back** `self.client.next_batch` to the previously-loaded token (or `""` if none), and call `mark_restored_sync_token_invalid()`. Only set `_first_sync_done = True` after the safe-vs-unsafe decision is recorded.
   - For non-first syncs, leave the existing fire-and-forget persistence (it pre-dates this PR — see §2). Soften the "cache before persisting" comment so it matches reality.

3. **Recover from `M_UNKNOWN_POS` (A-4 / B-3).**
   - Add `clear_sync_token(storage_path, agent_name)` in `src/mindroom/matrix/sync_tokens.py`.
   - In `_on_sync_error`, when `not self._first_sync_done` and status is `M_UNKNOWN_POS`: call `mark_restored_sync_token_invalid()`, set `self.client.next_batch = ""`, and call the new `clear_sync_token(...)` helper. Let nio's next iteration cold-sync naturally; do not raise.

4. **Empty-join guard (B-4).**
   - In the `else` branch of the first-sync gate, additionally require at least one joined room in the response before calling `mark_sync_catchup_applied()`. One extra `if joined_rooms:` is enough.

5. **Explicitly out of scope (file follow-ups, do not bundle):**
   - A-1 durable thread-cache checkpoint metadata.
   - A-3 non-first-sync persistence ordering.
   - PLAN-A §E `THREAD_CACHE_MAX_AGE_SECONDS` relaxation.
   - B-5 `Mapping` defensive check.
   - B-7 cold-start log emission.

## 6. Concise Test / Live-Test Strategy

### Unit tests (add to `tests/test_threading_error.py` and `tests/test_thread_history.py`)

| Test | Covers | Shape |
|------|--------|-------|
| `test_first_sync_cache_task_cancelled_does_not_trust_cache` | A-5 / B-1 | Stub `cache_sync_timeline` to return one pre-cancelled task; assert `pre_runtime_thread_cache_trusted` stays False, `mark_sync_catchup_applied` was not called, `mark_restored_sync_token_invalid` was. |
| `test_first_sync_cache_error_skips_token_persist_and_rolls_back` | A-2 / B-2 | Stub one task to raise `RuntimeError`; assert `_persist_sync_token` not called and `client.next_batch` reverts to the loaded value (or `""`). |
| `test_limited_first_sync_skips_token_persist_and_rolls_back` | A-2 (limited half) | Build response with one room having `timeline.limited=True`; assert same outcome as the cache-error test. |
| `test_unknown_pos_first_sync_clears_token_and_file` | A-4 / B-3 | Pre-write a sync token file, set `client.next_batch = "bad"`, fire `_on_sync_error` with `M_UNKNOWN_POS`; assert `client.next_batch == ""`, file is gone, `mark_restored_sync_token_invalid` called. |
| `test_first_sync_with_no_joined_rooms_does_not_trust_cache` | B-4 | `rooms.join = {}`; assert `pre_runtime_thread_cache_trusted` stays False. |

Keep the PR's two existing trust tests green (`test_restored_token_post_sync_reuses_pre_runtime_thread_cache`, `test_complete_first_sync_trusts_restored_thread_cache`) — they assert the happy path and should pass unchanged.

Run order on NixOS:

```bash
export NIX_PATH=nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos
nix-shell --run 'uv run pytest tests/test_thread_history.py tests/test_threading_error.py tests/test_event_cache.py tests/test_thread_mode.py -x -n 0 --no-cov -v'
nix-shell --run 'uv run pytest -x -n 0 --no-cov -v'
uv sync --all-extras && uv run pre-commit run --all-files
```

### Live test (local Synapse, per `CLAUDE.md`)

1. `just local-matrix-up`; `rm -f mindroom_data/matrix_state.yaml`; start MindRoom against the local stack.
2. **Happy path:** matty-send a thread, let it cache, stop, restart. Expect `matrix_sync_token_restored` + clean first sync + thread reply served from cache (no homeserver thread fetch in logs).
3. **Limited-timeline path:** before restart, push >50 messages from a second matty session into a joined room to force `timeline.limited=True`. Restart. Expect `matrix_sync_cache_catchup_limited`, **and** the saved `next_batch` is unchanged on disk between this start and the next, **and** the next restart still re-fetches thread history.
4. **M_UNKNOWN_POS path:** stop MindRoom, hand-edit `mindroom_data/matrix_state.yaml` to a stale token (e.g. `s9999_…`), restart. Expect `matrix_sync_token_rejected`, the saved file gone, and the bot to complete a cold first sync within the same process (no infinite loop).
5. Sanity-check via matty in all three scenarios:

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false uv run --python 3.13 matty send "Lobby" "Hello @mindroom_general:localhost please reply with pong."
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false uv run --python 3.13 matty threads "Lobby"
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false uv run --python 3.13 matty thread "Lobby" t1
```

Capture relevant `mindroom_data/logs/*.log` snippets for the PR description.

## 7. Bottom line

PLAN-A and PLAN-B converge on the same three must-fix bugs (A-2/B-2, A-4/B-3, A-5/B-1). PLAN-A is right that A-2's disk-side persistence is the load-bearing problem and that the M_UNKNOWN_POS recovery needs to delete the saved token file, not just clear memory. PLAN-A's broader checkpoint redesign (A-1) and non-first-sync ordering (A-3) are real concerns but disproportionate to ISSUE-197's scope; defer them. My empty-join guard (B-4) is a worthwhile cheap addition; my other defensive items (B-5, B-7) should be dropped.
