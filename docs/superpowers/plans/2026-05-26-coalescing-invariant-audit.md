# Coalescing Invariant Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make prompt-like Matrix ingress obey one receive-order, canonical-key, dispatch-target, and cleanup-owner invariant.

**Architecture:** Prompt-like ingress must create one owner before async work can escape.
`ConversationResolver` owns canonical thread resolution.
`CoalescingGate` owns receive order, debounce, upload grace, barriers, and drain.
`DispatchHandoff` owns converting the canonical `CoalescingKey` into Matrix relation state.
`TurnController` and `AgentBot` wire these owners without inventing side paths.

**Tech Stack:** Python 3.13, asyncio, pytest, MindRoom Matrix ingress pipeline.

---

## Repeatable Audit Checklist

- [ ] List every prompt-like Matrix callback before editing.
- [ ] For each callback, identify the first `await`.
- [ ] Prove receive-order ownership exists before that `await`, or add it.
- [ ] For each canonical-key lookup failure, prove sync certification becomes unsafe, or keep the event owned for replay.
- [ ] For each dispatch path, prove final response target comes from `CoalescingKey`, not stale Matrix relation content.
- [ ] For each ready task and metadata object, prove exactly one owner cancels, transfers, or closes it.
- [ ] For debounce and upload grace, prove unresolved reservations and barriers cannot widen or collapse user-facing windows incorrectly.
- [ ] For every accepted review finding, add a focused regression that fails before the fix.
- [ ] After fixes, run stale-symbol search for old ownership paths.
- [ ] After focused tests pass, run full pytest and pre-commit.

## Invariant Table

| Invariant | Owner | Must never happen |
| --- | --- | --- |
| Receive order is reserved before prompt-like async work | `TurnController` / `AgentBot` ingress boundary | A prompt-like callback awaits approval, thread lookup, media prep, or STT before reservation |
| Canonical key is authoritative | `ConversationResolver` | Failed or indeterminate thread lookup becomes `thread_id=None` |
| Gate owns batching | `CoalescingGate` | Controller decides debounce, upload grace, barriers, or cross-thread batching |
| Dispatch target follows batch key | `DispatchHandoff` | Single prepared event bypasses key-based relation normalization |
| Cleanup has one owner | `CoalescingGate` and `_PromptIngressReservationOwner` | Ready task or metadata can outlive released reservation |
| Certified checkpoint means no owned prompt work was lost | `AgentBot` sync shutdown | Callback exception is logged and checkpoint still saved as certified |

## Current Failing Rows To Patch

### Task 1: Ready Task Cleanup Owner

**Files:**
- Modify: `src/mindroom/turn_controller.py`
- Test: `tests/test_turn_controller.py`

- [ ] Add a regression that cancels `_PromptIngressReservationOwner.release()` while ready-task cleanup is between ownership removal and collection.
- [ ] Verify the test fails because the task is still running or metadata is not closed.
- [ ] Change `cancel_ready_task()` so it cancels before the first await.
- [ ] Add a done callback for late results if cleanup is itself cancelled.
- [ ] Run the new focused test.

### Task 2: Callback Failure Poisons Sync Certification

**Files:**
- Modify: `src/mindroom/bot.py`
- Test: `tests/test_matrix_sync_tokens.py`

- [ ] Add a regression where a Matrix callback raises after receiving a certified sync batch.
- [ ] Verify shutdown does not save a certified checkpoint after that callback failure.
- [ ] Mark sync certification unsafe in the callback wrapper when prompt callback errors escape.
- [ ] Run the new focused test.

### Task 3: Approval Fallthrough Reserves Before Async Work

**Files:**
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/turn_controller.py` if reservation handoff is needed.
- Test: `tests/test_live_message_coalescing.py` or `tests/test_turn_controller.py`

- [ ] Add a regression where an approval-looking text event awaits approval lookup, is not consumed, and a later normal text event arrives first.
- [ ] Verify receive order is inverted before the fix.
- [ ] Move approval handling behind the same early reservation owner, or reserve before approval and release if consumed.
- [ ] Run the new focused test.

### Task 4: Dispatch Handoff Uses CoalescingKey For All Source Kinds

**Files:**
- Modify: `src/mindroom/dispatch_handoff.py`
- Test: `tests/test_coalescing.py` or `tests/test_multi_agent_bot.py`

- [ ] Add a regression for one non-voice prepared event admitted under a non-null thread key with no `m.relates_to`.
- [ ] Verify the handoff lacks the canonical thread relation before the fix.
- [ ] Normalize missing or stale thread relations for every source kind when `CoalescingKey.thread_id` is non-null.
- [ ] Run the new focused test.

### Task 5: Upload Grace Preserves Solo Segments

**Files:**
- Modify: `src/mindroom/coalescing.py`
- Test: `tests/test_coalescing.py`

- [ ] Add a regression with upload grace enabled and a ready task that resolves to `requires_solo_batch=True`.
- [ ] Verify the solo event is not flattened into a mixed `build_coalesced_batch()` call.
- [ ] Skip upload grace for claims containing solo segments, or apply grace only to non-solo segment without flattening.
- [ ] Run the new focused test.

### Task 6: In-Flight Debounce Marker Is Precise

**Files:**
- Modify: `src/mindroom/coalescing.py`
- Test: `tests/test_coalescing.py`

- [ ] Add a regression where later queued messages arrive during unresolved-reservation wait, not during in-flight dispatch.
- [ ] Verify they are incorrectly batched before the fix.
- [ ] Record the queue/order boundary when entering `IN_FLIGHT`.
- [ ] Only ignore debounce gaps for admissions created after that boundary and before dispatch completes.
- [ ] Run the new focused test.

## Final Verification

- [ ] Run focused tests for all six tasks with `-n 0 --no-cov -q`.
- [ ] Run stale-symbol search for `key_hint|target_key_task|dispatch_key|retarget|_key_aliases`.
- [ ] Run `uv run pytest -n auto --no-cov -q`.
- [ ] Run `uv run pre-commit run --all-files`.
- [ ] Update PR body with exact test counts and invariant wording.

## Execution Status: 2026-05-26

- [x] Added focused regressions for accepted review rows.
- [x] Fixed ready-task cleanup ownership.
- [x] Made callback failures block certified sync checkpoints.
- [x] Reserved active approval fallthrough before async approval lookup.
- [x] Made dispatch handoff normalize stale relations from canonical `CoalescingKey`, while preserving first-turn root semantics.
- [x] Verified upload-grace solo metadata path with focused coverage.
- [x] Made in-flight debounce buffering use an exact in-flight order boundary.
- [x] Ran focused task tests.
- [x] Ran stale-symbol search.
- [x] Ran affected suite.
- [x] Ran full pytest.
- [x] Ran pre-commit.
