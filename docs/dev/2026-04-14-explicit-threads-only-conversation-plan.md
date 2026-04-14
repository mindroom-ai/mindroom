# Explicit Threads Only Conversation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove reply-chain-to-thread inference so MindRoom treats only explicit `m.thread` relations as threaded conversations while preserving plain reply UX.

**Architecture:** Delete `matrix/reply_chain.py` and collapse conversation resolution onto direct thread metadata only. Keep plain `m.in_reply_to` for immediate reply targeting, but stop using it for thread identity, thread-history reads, cache invalidation, or synthetic conversation roots.

**Tech Stack:** Python 3.13, Matrix nio events, existing `MatrixConversationCache`, pytest, pre-commit.

---

## File Map

- Delete: `src/mindroom/matrix/reply_chain.py`
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/cache/thread_writes.py`
- Modify: `src/mindroom/thread_tags.py`
- Modify: `src/mindroom/inbound_turn_normalizer.py`
- Modify: `src/mindroom/commands/handler.py`
- Modify: `src/mindroom/turn_controller.py`
- Test: `tests/test_threading_error.py`
- Test: `tests/test_thread_mode.py`
- Test: `tests/test_thread_tags.py`
- Test: `tests/test_preformed_team_routing.py`
- Test: any other reply-chain-specific tests surfaced by `rg -n "reply_chain|canonicalize_related_event_id|requires_full_thread_history|m\\.in_reply_to" tests`

### Task 1: Freeze the New Product Rule in Tests

**Files:**
- Modify: `tests/test_threading_error.py`
- Modify: `tests/test_thread_mode.py`
- Modify: `tests/test_preformed_team_routing.py`

- [ ] **Step 1: Write failing tests for the new explicit-thread rule**

Add tests that prove:
- a plain `m.in_reply_to` event without `m.thread` yields `is_thread=False`, `thread_id=None`, and empty `thread_history`
- a plain reply to a threaded message is still treated as a plain reply
- direct-thread preview still sets `requires_full_thread_history=True` only for explicit threads

- [ ] **Step 2: Run the targeted tests to verify failure**

Run: `uv run pytest tests/test_threading_error.py tests/test_thread_mode.py tests/test_preformed_team_routing.py -k 'plain reply or explicit thread or requires_full_thread_history' -x -n 0 --no-cov -q`

Expected: failures showing the old reply-chain behavior is still active.

- [ ] **Step 3: Commit the failing-test checkpoint**

```bash
git add tests/test_threading_error.py tests/test_thread_mode.py tests/test_preformed_team_routing.py
git commit -m "test: lock explicit-thread-only conversation behavior"
```

### Task 2: Remove Reply-Chain Resolution from ConversationResolver

**Files:**
- Modify: `src/mindroom/conversation_resolver.py`
- Delete: `src/mindroom/matrix/reply_chain.py`

- [ ] **Step 1: Remove reply-chain imports and state from the resolver**

Delete `ReplyChainCaches`, `canonicalize_related_event_id`, `derive_conversation_context`, and `derive_conversation_target` imports.
Remove the `reply_chain` field from `ConversationResolver`.

- [ ] **Step 2: Replace resolver methods with direct-thread-only logic**

Implement:
- `coalescing_thread_id()` returns only explicit `thread_id` or `thread_id_from_edit`
- `derive_conversation_context()` returns history only for explicit threads
- `derive_conversation_target()` returns snapshot plus `requires_full_thread_history` only for explicit threads
- plain replies return non-thread context with no history

- [ ] **Step 3: Run focused resolver tests**

Run: `uv run pytest tests/test_threading_error.py tests/test_thread_mode.py -k 'conversation_context or dispatch_context or coalescing_thread_id' -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 4: Commit the resolver simplification**

```bash
git add src/mindroom/conversation_resolver.py src/mindroom/matrix/reply_chain.py tests/test_threading_error.py tests/test_thread_mode.py
git commit -m "refactor: drop reply-chain conversation inference"
```

### Task 3: Remove Reply-Chain Hooks from the Cache Layer

**Files:**
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/cache/thread_writes.py`

- [ ] **Step 1: Delete reply-chain cache binding from `conversation_cache.py`**

Remove `ReplyChainCaches` imports, `_reply_chain_caches_getter`, `bind_reply_chain_caches()`, and `_reply_chain_caches()`.

- [ ] **Step 2: Delete reply-chain invalidation from `thread_writes.py`**

Remove:
- `reply_chain_caches_getter`
- `_reply_chain_caches()`
- `_invalidate_reply_chain()`
- redaction/edit helpers whose only purpose is reply-chain invalidation

Keep thread invalidation and durable event cache writes intact.

- [ ] **Step 3: Run cache-write tests**

Run: `uv run pytest tests/test_threading_error.py -k 'redaction or outbound_edit or reset_runtime_state or cache_write' -x -n 0 --no-cov -q`

Expected: PASS after removing reply-chain-specific assertions and dead setup.

- [ ] **Step 4: Commit the cache-layer cleanup**

```bash
git add src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/thread_writes.py tests/test_threading_error.py
git commit -m "cleanup: remove reply-chain cache invalidation"
```

### Task 4: Simplify Thread Tags and Callers

**Files:**
- Modify: `src/mindroom/thread_tags.py`
- Modify: `src/mindroom/inbound_turn_normalizer.py`
- Modify: `src/mindroom/commands/handler.py`

- [ ] **Step 1: Remove thread-root normalization by plain reply traversal**

Update `normalize_thread_root_event_id()` so it only accepts explicit thread roots or explicit thread replies.
Return `None` for plain replies that do not identify a real thread root.

- [ ] **Step 2: Adjust callers that expected plain-reply thread inference**

Audit inbound normalization and command handling.
Make any plain-reply path fall back to non-thread behavior instead of inferred-thread behavior.

- [ ] **Step 3: Run focused caller and thread-tag tests**

Run: `uv run pytest tests/test_thread_tags.py tests/test_threading_error.py tests/test_unknown_command_response.py -k 'thread tag or plain reply or command' -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 4: Commit caller simplification**

```bash
git add src/mindroom/thread_tags.py src/mindroom/inbound_turn_normalizer.py src/mindroom/commands/handler.py tests/test_thread_tags.py tests/test_threading_error.py tests/test_unknown_command_response.py
git commit -m "refactor: stop inferring thread roots from plain replies"
```

### Task 5: Remove Dead Preview and Routing Branches

**Files:**
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `tests/test_thread_mode.py`
- Modify: `tests/test_multi_agent_bot.py`

- [ ] **Step 1: Delete dead plain-reply preview assumptions**

Remove branches that only exist because plain replies could become thread previews.
Keep `requires_full_thread_history` only for explicit-thread snapshot flows.

- [ ] **Step 2: Update dispatch tests**

Replace reply-chain preview assertions with explicit-thread-only assertions.
Delete tests that only verify reply-chain promotion, chain merging, or synthetic root retention.

- [ ] **Step 3: Run focused dispatch tests**

Run: `uv run pytest tests/test_thread_mode.py tests/test_multi_agent_bot.py tests/test_threading_error.py -k 'dispatch_context or requires_full_thread_history or plain reply or explicit thread' -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 4: Commit the dispatch cleanup**

```bash
git add src/mindroom/turn_controller.py src/mindroom/conversation_resolver.py tests/test_thread_mode.py tests/test_multi_agent_bot.py tests/test_threading_error.py
git commit -m "cleanup: remove plain-reply thread preview paths"
```

### Task 6: Delete Dead Tests and Run Full Verification

**Files:**
- Modify: reply-chain-related test files found by search

- [ ] **Step 1: Delete reply-chain-only tests**

Remove tests that cover:
- reply-chain traversal depth
- cycle handling
- synthetic stable plain-reply roots
- reply-chain cache invalidation
- plain-reply-to-thread promotion
- merged chain-plus-thread history

- [ ] **Step 2: Run the narrowed focused suite**

Run: `uv run pytest tests/test_threading_error.py tests/test_thread_mode.py tests/test_thread_tags.py tests/test_preformed_team_routing.py tests/test_multi_agent_bot.py -x -n 0 --no-cov -q`

Expected: PASS

- [ ] **Step 3: Run full verification**

Run:
- `uv run pytest -n auto --no-cov -q`
- `uv run pre-commit run --all-files`

Expected:
- pytest passes
- pre-commit passes

- [ ] **Step 4: Commit the branch cleanup**

```bash
git add src/mindroom/conversation_resolver.py src/mindroom/matrix/conversation_cache.py src/mindroom/matrix/cache/thread_writes.py src/mindroom/thread_tags.py src/mindroom/inbound_turn_normalizer.py src/mindroom/commands/handler.py src/mindroom/turn_controller.py tests/test_threading_error.py tests/test_thread_mode.py tests/test_thread_tags.py tests/test_preformed_team_routing.py tests/test_multi_agent_bot.py
git add -u src/mindroom/matrix/reply_chain.py
git commit -m "refactor: require explicit Matrix threads"
```
