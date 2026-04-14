# Matrix Conversation Cache Architecture Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the Matrix conversation-cache refactor by sealing the remaining authority, visibility, and lifecycle seams.

**Architecture:** Keep the existing split between `_event_cache.py`, `thread_cache.py`, `thread_reads.py`, `thread_writes.py`, and `MatrixConversationCache`, but tighten the contracts between them. Outbound write-through must use the exact delivered Matrix payload, advisory cache work must fail open, runtime-local conversation state must reset in one place, and authoritative durable writes must obey one room-ordered visibility contract.

**Tech Stack:** Python 3.13, asyncio, SQLite, matrix-nio, pytest, Ruff, pre-commit.

---

## Scope

This is a closure pass, not a fresh redesign.
The file split already landed.
The remaining work is to make the current boundaries true in behavior, not just in file layout.

## Files

- Modify: `src/mindroom/matrix/client.py`
- Modify: `src/mindroom/matrix/large_messages.py` if helper extraction is needed
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/thread_reads.py`
- Modify: `src/mindroom/matrix/thread_writes.py`
- Modify: `src/mindroom/matrix/_event_cache.py`
- Modify: `src/mindroom/matrix/_event_cache_write_coordinator.py`
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/hooks/sender.py`
- Modify: `src/mindroom/scheduling.py`
- Modify: `src/mindroom/thread_summary.py`
- Modify: `src/mindroom/custom_tools/matrix_api.py`
- Modify: `src/mindroom/custom_tools/matrix_message.py`
- Modify: `src/mindroom/custom_tools/subagents.py`
- Modify: `tests/test_large_messages_integration.py`
- Modify: `tests/test_streaming_behavior.py`
- Modify: `tests/test_send_file_message.py`
- Modify: `tests/test_thread_summary.py`
- Modify: `tests/test_matrix_api_tool.py`
- Modify: `tests/test_scheduling.py`
- Modify: `tests/test_workflow_scheduling.py`
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_multi_agent_bot.py`
- Modify: `docs/superpowers/specs/2026-04-13-matrix-conversation-cache-architecture-closure-design.md`
- Delete: `docs/superpowers/specs/2026-04-13-matrix-conversation-cache-response-path-simplification-design.md`
- Delete: `docs/superpowers/plans/2026-04-13-matrix-conversation-cache-architecture.md`
- Delete: `docs/superpowers/plans/2026-04-13-matrix-conversation-cache-response-path-simplification.md`

## Task 1: Lock The Remaining Seams With Failing Tests

- [ ] Add one large-message send regression proving write-through stores the exact transformed `content_sent`.
- [ ] Add one large-edit regression proving write-through stores the exact transformed edit payload.
- [ ] Add one scheduling regression proving successful send stays successful when advisory cache write-through fails.
- [ ] Add one room-idle regression proving unrelated reads do not fail on advisory queued write failure.
- [ ] Add one post-lock refresh regression proving authoritative refresh failures still route through handled dispatch failure.
- [ ] Add one runtime-reset regression proving reply-chain caches are cleared on restart/reset.
- [ ] Add one point-lookup regression proving event lookup cache fills do not mutate authoritative thread/edit indexes outside the room barrier.
- [ ] Run only the new/changed tests with `uv run pytest -n auto --no-cov <nodeids> -q`.
- [ ] Confirm they fail for the intended reason before implementation.

## Task 2: Deliver Exact Matrix Payload Truth

- [ ] Introduce a typed `DeliveredMatrixEvent` result in `src/mindroom/matrix/client.py`.
- [ ] Change `send_message()` to return `DeliveredMatrixEvent | None` and include `content_sent` after `prepare_large_message(...)`.
- [ ] Change `edit_message()` to return the same typed result.
- [ ] Update all outbound send/edit callers to use the returned delivered payload for cache write-through.
- [ ] Remove any remaining write-through call site that caches the pre-send draft payload instead of the delivered payload.
- [ ] Run targeted send/edit tests with `uv run pytest -n auto --no-cov tests/test_large_messages_integration.py tests/test_streaming_behavior.py tests/test_send_file_message.py tests/test_thread_summary.py tests/test_matrix_api_tool.py -q`.

## Task 3: Make Advisory Cache Bookkeeping Truly Fail Open

- [ ] Make `MatrixConversationCache.record_outbound_message()` swallow and log advisory failures at the public boundary.
- [ ] Make `MatrixConversationCache.record_outbound_redaction()` do the same.
- [ ] Keep `thread_writes.py` advisory queue tasks non-raising after successful delivery.
- [ ] Make `_EventCacheWriteCoordinator.wait_for_room_idle()` a visibility barrier only, not an exception transport.
- [ ] Remove redundant per-caller fail-open wrappers when the public cache contract is enough.
- [ ] Re-run targeted delivery and scheduling tests with `uv run pytest -n auto --no-cov tests/test_scheduling.py tests/test_workflow_scheduling.py tests/test_threading_error.py tests/test_matrix_api_tool.py tests/test_thread_summary.py tests/test_send_file_message.py -q`.

## Task 4: Normalize Authoritative Post-Lock Failure Handling

- [ ] Wrap post-lock thread refresh inside the same normalized error boundary as `prepare_after_lock`.
- [ ] Keep `TurnController` handling one post-lock exception type only.
- [ ] Add or update regression coverage in `tests/test_multi_agent_bot.py` or `tests/test_threading_error.py`.
- [ ] Re-run the focused post-lock tests with `uv run pytest -n auto --no-cov tests/test_multi_agent_bot.py tests/test_threading_error.py -q`.

## Task 5: Give Runtime Conversation State One Reset Owner

- [ ] Add explicit reply-chain cache reset support to the runtime reset seam.
- [ ] Make `reset_runtime_state()` clear both resolved-thread state and reply-chain caches.
- [ ] Verify shutdown and same-instance restart paths still use that reset seam.
- [ ] Re-run the restart/reset tests with `uv run pytest -n auto --no-cov tests/test_threading_error.py tests/test_multi_agent_bot.py -q`.

## Task 6: Finish The Room-Ordered Visibility Contract

- [ ] Decide point-lookup semantics explicitly in `_cached_room_get_event()`.
- [ ] Implement the chosen rule so point lookups only persist through the room barrier and never bypass visibility ordering.
- [ ] If schema or cache layout changes are needed, make stale cache reset explicit in comments and code instead of migrating.
- [ ] Re-run focused event-cache and thread-history tests with `uv run pytest -n auto --no-cov tests/test_event_cache.py tests/test_thread_history.py tests/test_threading_error.py -q`.

## Task 7: Thin The Facade For Real

- [ ] Remove private forwarding helpers from `conversation_cache.py` that exist only to mirror `thread_reads.py` or `thread_writes.py`.
- [ ] Replace broad reach-through from `thread_reads.py` and `thread_writes.py` into cache internals with a smaller explicit coordination surface where it reduces net complexity.
- [ ] Keep `MatrixConversationCache` as the single public API.
- [ ] Re-read the touched modules and simplify before final verification if wrapper count has grown.

## Task 8: Align The Docs

- [ ] Delete the superseded response-path and extraction-history docs once the closure doc is accurate.
- [ ] Keep one sentence per line.
- [ ] Make the no-migration cache policy explicit in the closure spec and any schema/version comments.

## Task 9: Full Verification And Commit

- [ ] Run `uv run pytest -n auto --no-cov -q`.
- [ ] Run `uv run pre-commit run --all-files`.
- [ ] Review `git diff --stat` and simplify any wrapper-heavy fallout before commit.
- [ ] Commit with a message that reflects architecture closure rather than another local patch.
