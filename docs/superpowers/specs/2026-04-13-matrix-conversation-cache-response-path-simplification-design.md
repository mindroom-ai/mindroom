# Matrix Conversation Cache Response-Path Simplification Design

**Status:** Partly implemented, with follow-on architecture extraction already landed.
**Date:** 2026-04-13.
**Owner:** Codex.

## Purpose

Simplify the Matrix reply-generation path so it is easier to reason about and harder to make stale-context mistakes.
Keep the durable and cross-turn caches that ISSUE-128, ISSUE-139, and ISSUE-143 showed are necessary.
Remove the fragile optimization where response generation trusts a pre-lock `ThreadHistoryResult` after a post-lock currentness check.

## Background

The current PR cleaned up the cache boundary and ownership story, but it did not fully simplify the reply path.
`MatrixConversationCache` is now the primary public boundary for Matrix conversation data.
However, response generation still fetches thread context before acquiring the per-thread lifecycle lock and then tries to prove that earlier history is still current.
That currentness shortcut depends on several subtle invariants.
Those invariants have produced repeated review findings around stale histories, write barriers, and freshness bookkeeping.

The issue reports support keeping the cache layers while simplifying the response path.
ISSUE-128 showed that cache-first event reads are required for correctness in some rooms.
ISSUE-139 showed that cache-aware latest-thread-event lookups are required for performance.
ISSUE-143 showed that cross-turn thread caching materially improves latency.
ISSUE-147 showed that the main design smell is split freshness ownership and incomplete boundary enforcement, not the mere existence of multiple caches.

## Problem Statement

The reply path currently mixes two different ideas of authority.
Before the lifecycle lock, request construction may fetch thread snapshots or full history for routing and prompt preparation.
After the lock, `ResponseRunner` can skip a fresh thread read by calling `is_thread_history_current()` on that earlier history.
That means a reply can depend on both pieces of state at once.
- The request can carry pre-lock thread history.
- The post-lock freshness bookkeeping can try to prove that history is still valid.

This creates avoidable complexity.
It also creates race surface whenever writes, sync persistence, repairs, or lookup-repair promotion happen between the initial read and the locked response start.

## Design Goals

- Keep `MatrixConversationCache` as the one public boundary for Matrix conversation reads.
- Keep the durable SQLite-backed event and repair state.
- Keep cross-turn resolved thread reuse.
- Make reply generation use one authoritative thread-history read at the moment a reply actually starts.
- Prefer simplicity and robustness over the last bit of latency optimization.

## Non-Goals

- Do not remove the durable event cache.
- Do not remove sync-populated thread cache writes.
- Do not remove cache-aware latest-thread-event lookup for outbound MSC3440 fallback.
- Do not collapse all Matrix conversation reads into a single API in this step.
- Do not do the file-extraction refactor in this step unless it falls out naturally from the simplification.

## Recommended Approach

For response generation, stop treating pre-lock thread history as authoritative.
Before the per-thread lifecycle lock, resolve only the conversation identity needed for routing, targeting, and reply-chain handling.
If the lightweight planning snapshot declares itself incomplete, hydrate full history before responder or team policy runs so planning does not decide from weaker thread state.
After the lifecycle lock is acquired, fetch full thread history exactly once through `MatrixConversationCache`.
Use that single post-lock result for the rest of the reply.

This removes the need for `ResponseRunner` to ask whether an older `ThreadHistoryResult` is still current.
It also means that the full reply path only trusts thread context that was read after queued room writes were drained and after the reply turn owns the lifecycle lock.

## Data Ownership

The ownership model remains the same.
- `_event_cache.py` owns durable event truth, edit indexes, durable unresolved lookup repairs, and durable thread-repair-required state.
- `thread_cache.py` owns process-local resolved-thread reuse, locks, and generation metadata.
- `_event_cache_write_coordinator.py` owns room-ordered visibility for freshness-affecting writes.
- `conversation_cache.py` owns orchestration and policy across those layers.

Stale on-disk cache schemas are intentionally discarded rather than migrated.
The Matrix event cache is treated as rebuildable advisory state, so old SQLite cache contents are dropped and repopulated lazily through normal usage.

What changes is not the storage model.
What changes is the reply-path contract.

## Reply Path Before And After

### Before

1. Extract context and possibly fetch a thread snapshot or full history before the lifecycle lock.
2. Attach that `thread_history` to the `ResponseRequest`.
3. Acquire the lifecycle lock.
4. Call `is_thread_history_current()` to see whether the earlier history can be reused.
5. Only refetch if that currentness check says the earlier history is stale.

### After

1. Extract routing and targeting context before the lifecycle lock.
2. If the planning snapshot is incomplete, hydrate full history before responder or team policy runs.
3. Do not attach pre-lock thread history as authoritative reply context.
4. Acquire the lifecycle lock.
5. Fetch full thread history once through `MatrixConversationCache.get_thread_history(...)`.
6. Use that single post-lock history for prompt preparation, memory context, and response generation.

## Practical Performance Expectations

This design does not imply a Matrix network read on every reply.
The authoritative post-lock read still goes through `MatrixConversationCache`.
In warm threads, that read should typically be served from the resolved in-memory thread cache or from SQLite-backed cached thread events.
Matrix history calls should still happen only on cold miss, repair-required threads, or known-incomplete cache state.

The main tradeoff is one more local authoritative cache read in the reply path.
In exchange, the code no longer depends on proving that an earlier request-scoped history object is still current.

## Consequences For Existing APIs

`is_thread_history_current()` is no longer needed in the response-generation path.
It may remain temporarily for non-response use cases until those call sites are evaluated.
`get_thread_snapshot()` may also remain for non-response paths.
This step does not require deleting snapshot support everywhere.

The important boundary is that reply generation must not trust pre-lock `thread_history`.

## Freshness And Write Ordering

This simplification works best when every freshness-affecting write is visible after the same room-ordered barrier.
- Live append or redaction persistence must become visible after the same room-idle barrier that `MatrixConversationCache` reads trust.
- Durable lookup-repair writes must become visible after the same room-idle barrier that `MatrixConversationCache` reads trust.
- Sync thread persistence must become visible after the same room-idle barrier that `MatrixConversationCache` reads trust.
- Any mutation that changes whether cached thread history is trustworthy must become visible after the same room-idle barrier that `MatrixConversationCache` reads trust.

That requirement already exists implicitly in the current design.
This simplification makes it explicit.

## Implementation Scope

### Required

- Remove `ResponseRunner` use of `is_thread_history_current()` for post-lock refresh skipping.
- Change reply generation so full thread history is fetched once after the lifecycle lock.
- Stop treating pre-lock request `thread_history` as authoritative for normal reply generation.
- Keep `MatrixConversationCache` as the only read boundary used by the post-lock fetch.
- Ensure freshness-affecting writes that can influence the post-lock read go through the shared room-ordered barrier.

### Deferred

- Deleting `get_thread_snapshot()` entirely.
- Deleting `is_thread_history_current()` entirely.
- Wider cleanup of non-response callers that may still use snapshots appropriately.

## Testing Strategy

Add or adjust tests to prove the following behaviors.
- Incomplete planning snapshots are hydrated before policy decisions run.
- Response generation performs one authoritative post-lock thread-history read.
- Stale pre-lock history on the request is ignored.
- Warm-thread replies still read through `MatrixConversationCache` without forcing Matrix network fetches.
- Live writes that affect freshness become visible before the post-lock read is trusted.
- Repaired or dirty threads still do the right thing through the cache boundary.

Where tests need a conversation cache, they should install the real required runtime support pattern rather than mocking around it.

## Relationship To The Existing Plan

This design intentionally narrowed and partly superseded the earlier extraction-first plan in `docs/superpowers/plans/2026-04-13-matrix-conversation-cache-architecture.md`.
The reply-path simplification has landed in its important form:
- locked reply execution refreshes authoritative thread history after the lifecycle lock,
- `ResponseRunner` no longer relies on the old currentness shortcut,
- incomplete planning snapshots are still hydrated before policy when stronger thread state is required.

The later `thread_reads.py` / `thread_writes.py` extraction has also already landed.
The remaining work is therefore not "split the file later".
It is to preserve the same authority rules across the extracted modules and close any remaining bypasses.

The decomposition step should preserve the reply-path invariant from this spec.
- Normal reply generation trusts one authoritative post-lock `get_thread_history(...)` read.
- Any authoritative thread question must go through one repair-aware read path.
- Any local thread-affecting send or edit must go through one cache write-through path.
- Local redactions must also go through that same cache-consistent mutation contract.
- Non-authoritative or degraded reads may be returned as one-shot fallbacks, but must never be stored as reusable authoritative cache entries.

That means the file split is not a purely cosmetic extraction.
It is the point where the remaining bypasses must be removed so future changes cannot reintroduce the same architectural smell.

## Recommendation

Implement the reply-path simplification first.
Do not spend more time preserving the currentness shortcut through increasingly complex freshness bookkeeping.
Keep the caches that the issue reports showed are valuable.
Remove the reply-path optimization that keeps making correctness harder to guarantee.
