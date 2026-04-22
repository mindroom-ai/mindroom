# Terminal Delivery Contract Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the full terminal-delivery migration so one canonical policy table and one typed caller-facing lifecycle result govern hooks, visibility, response identity, turn completion, retryability, and final-text authority.

**Architecture:** Introduce a pure canonical-delivery module first, then migrate gateway, lifecycle, post-effects, callers, and final-text consumers in phases that each remove one old inference seam. The implementation must replace `str | None`, `DeliveryResult`-driven semantics, and truthy event-id success inference with a policy-derived `TurnDeliveryResolution`, while keeping the gateway as the sole terminal coordinator.

**Tech Stack:** Python 3.12, asyncio, dataclasses, Matrix nio helpers, pytest, existing MindRoom streaming and hook abstractions.

**Status:** Completed on `pr-issue-178-terminal-status` at `df8dc4dbd` after the targeted terminal-delivery verification suites and scoped pre-commit checks passed.

---

## Accountability Protocol

This migration fails if we merely add new types while old semantic escape hatches survive.
Each task therefore has a hard exit condition beyond "tests pass."

### Non-Negotiable Gates

- The policy table in `src/mindroom/final_delivery.py` is the single source of truth for:
  - `visible_response_event_id`
  - `response_identity_event_id`
  - `turn_completion_event_id`
  - hook policy
  - handledness
  - retryability
- No production code is written before a failing test exists for that behavior.
- No task is complete until it removes one old inference seam, not just wraps it.
- No caller may use `if event_id is not None` to decide success.
- No terminal hook emission may remain outside `delivery_gateway.py`.
- No exception may bypass the typed terminal boundary after delivery has started.
- No ambiguous first visible terminal send may auto-retry.

### Stop-The-Line Checks

Run these after each major phase.
If any output contradicts the phase goal, stop and fix the phase before continuing.

```bash
rg -n "resolve_response_event_id|_is_cancelled_delivery_result" src/mindroom
rg -n "emit_cancelled_response|emit_after_response" src/mindroom/response_runner.py src/mindroom/response_lifecycle.py
rg -n "-> str \\| None|Awaitable\\[str \\| None\\]" src/mindroom/bot.py src/mindroom/turn_controller.py src/mindroom/edit_regenerator.py src/mindroom/commands/handler.py src/mindroom/response_runner.py
rg -n "event_id is not None|response_event_id is not None|raw_event_id" src/mindroom/bot.py src/mindroom/turn_controller.py src/mindroom/edit_regenerator.py src/mindroom/commands/handler.py
```

Expected by the end:
- no lifecycle semantic helpers remain
- no runner/lifecycle hook emission remains
- no outward-facing control-flow APIs rely on `str | None`
- no caller decides success from event-id truthiness

## File Map

**Create:**

- `src/mindroom/final_delivery.py`
- `tests/test_final_delivery.py`

**Modify:**

- `src/mindroom/delivery_gateway.py`
- `src/mindroom/response_lifecycle.py`
- `src/mindroom/post_response_effects.py`
- `src/mindroom/response_runner.py`
- `src/mindroom/streaming.py`
- `src/mindroom/ai.py`
- `src/mindroom/teams.py`
- `src/mindroom/api/openai_compat.py`
- `src/mindroom/bot.py`
- `src/mindroom/turn_controller.py`
- `src/mindroom/edit_regenerator.py`
- `src/mindroom/commands/handler.py`
- `tests/test_streaming_finalize.py`
- `tests/test_cancelled_response_hook.py`
- `tests/test_ai_user_id.py`
- `tests/test_queued_message_notify.py`
- `tests/test_multi_agent_bot.py`

## Task 1: Freeze Canonical Policy First

**Files:**
- Create: `src/mindroom/final_delivery.py`
- Create: `tests/test_final_delivery.py`

- [x] **Step 1: Write failing policy-table tests for every terminal state.**

Cover at minimum:
- hook emission policy
- `visible_response_event_id`
- `response_identity_event_id`
- `turn_completion_event_id`
- handledness
- retryability
- interactive eligibility
- persistence eligibility
- shielding eligibility

- [x] **Step 2: Run the new policy-table tests and verify they fail for missing module/types.**

Run:
```bash
uv run pytest tests/test_final_delivery.py -q -n 0 --no-cov
```

Expected:
- FAIL because `final_delivery.py` and canonical policy types do not exist yet

- [x] **Step 3: Implement `FinalDeliveryOutcome`, per-state policy rows, and accessor helpers in `src/mindroom/final_delivery.py`.**

Requirements:
- frozen dataclasses only
- policy rows derive all three event-id meanings
- no hand-coded parallel conditionals outside the policy table

- [x] **Step 4: Re-run `tests/test_final_delivery.py` until green.**

Run:
```bash
uv run pytest tests/test_final_delivery.py -q -n 0 --no-cov
```

Expected:
- PASS

- [x] **Step 5: Commit the canonical policy layer.**

```bash
git add src/mindroom/final_delivery.py tests/test_final_delivery.py
git commit -m "refactor: add canonical terminal delivery policy"
```

## Task 2: Introduce `TurnDeliveryResolution` And Constrain Lifecycle Repair

**Files:**
- Modify: `src/mindroom/response_lifecycle.py`
- Modify: `src/mindroom/final_delivery.py`
- Test: `tests/test_final_delivery.py`
- Test: `tests/test_cancelled_response_hook.py`

- [x] **Step 1: Write failing lifecycle tests for `TurnDeliveryResolution`.**

Cover at minimum:
- lifecycle returns typed result instead of `str | None`
- `state` matches `FinalDeliveryOutcome.state`
- outer repair cannot synthesize `response_identity_event_id`
- outer repair can preserve already-known visibility without changing semantics

- [x] **Step 2: Run only the new lifecycle tests and confirm failure.**

Run:
```bash
uv run pytest tests/test_final_delivery.py tests/test_cancelled_response_hook.py -q -n 0 --no-cov -k "turn_delivery_resolution or outer_repair"
```

Expected:
- FAIL with missing type or wrong lifecycle return shape

- [x] **Step 3: Implement `TurnDeliveryResolution` and refactor `ResponseLifecycle.finalize()` to return it.**

Requirements:
- raw `FinalDeliveryOutcome` does not cross to outer callers
- outer repair is transport-only
- no hook emission in lifecycle

- [x] **Step 4: Re-run the targeted lifecycle tests until green.**

Run:
```bash
uv run pytest tests/test_final_delivery.py tests/test_cancelled_response_hook.py -q -n 0 --no-cov -k "turn_delivery_resolution or outer_repair"
```

Expected:
- PASS

- [x] **Step 5: Run the stop-the-line checks for lifecycle helpers.**

Run:
```bash
rg -n "resolve_response_event_id|_is_cancelled_delivery_result" src/mindroom
```

Expected:
- these helpers still exist if later tasks need them, but any remaining hits are clearly on the deletion path and not used by migrated lifecycle code

- [x] **Step 6: Commit the lifecycle result boundary.**

```bash
git add src/mindroom/final_delivery.py src/mindroom/response_lifecycle.py tests/test_final_delivery.py tests/test_cancelled_response_hook.py
git commit -m "refactor: return typed terminal lifecycle resolution"
```

## Task 3: Move All Terminal Semantics And Hooks Into `delivery_gateway.py`

**Files:**
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/response_runner.py`
- Test: `tests/test_cancelled_response_hook.py`
- Test: `tests/test_streaming_finalize.py`

- [x] **Step 1: Write failing tests for gateway-owned terminal hook emission.**

Cover at minimum:
- ordinary non-streaming failed send emits exactly one terminal hook
- ordinary non-streaming failed edit emits exactly one terminal hook
- streamed terminal failure emits exactly one terminal hook
- `suppression_cleanup_failed` stays in the typed contract after delivery starts
- preserved-stream re-edit failure does not redact the already-visible stream

- [x] **Step 2: Run the targeted gateway tests and verify failure.**

Run:
```bash
uv run pytest tests/test_cancelled_response_hook.py tests/test_streaming_finalize.py -q -n 0 --no-cov -k "terminal_hook or suppression_cleanup_failed or preserved_stream"
```

Expected:
- FAIL on current hook emission or cleanup behavior

- [x] **Step 3: Refactor `delivery_gateway.py` so it is the only terminal hook owner.**

Requirements:
- canonical outcome for every final send/edit failure
- no runner fallback hook emission
- no exception escape after delivery has started
- preserved-stream states keep the event physically visible

- [x] **Step 4: Remove matching runner/lifecycle terminal-hook emission.**

Requirements:
- no duplicate hook emission paths
- no hook backfill from raw `DeliveryResult`

- [x] **Step 5: Re-run the gateway tests until green.**

Run:
```bash
uv run pytest tests/test_cancelled_response_hook.py tests/test_streaming_finalize.py -q -n 0 --no-cov -k "terminal_hook or suppression_cleanup_failed or preserved_stream"
```

Expected:
- PASS

- [x] **Step 6: Run the stop-the-line hook grep.**

Run:
```bash
rg -n "emit_cancelled_response|emit_after_response" src/mindroom/response_runner.py src/mindroom/response_lifecycle.py
```

Expected:
- no remaining terminal hook emission in runner/lifecycle

- [x] **Step 7: Commit gateway ownership.**

```bash
git add src/mindroom/delivery_gateway.py src/mindroom/response_runner.py src/mindroom/response_lifecycle.py tests/test_cancelled_response_hook.py tests/test_streaming_finalize.py
git commit -m "refactor: centralize terminal delivery hooks in gateway"
```

## Task 4: Unify Final Text And Streaming Transport Rules

**Files:**
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/ai.py`
- Modify: `src/mindroom/teams.py`
- Modify: `src/mindroom/api/openai_compat.py`
- Test: `tests/test_ai_user_id.py`
- Test: `tests/test_streaming_finalize.py`

- [x] **Step 1: Write failing tests for final-text authority.**

Cover at minimum:
- corrective `RunCompletedEvent.content` overrides earlier partial text
- empty-string final completion is authoritative
- `RunCompletedEvent.content is None` preserves earlier partial text
- agent and team paths persist the same final text
- SSE emits the same final text as Matrix delivery
- hidden-tool-only completion cannot finish as visible `Thinking...`
- ambiguous first visible terminal send is not retried

- [x] **Step 2: Run only the new text/stream tests and confirm failure.**

Run:
```bash
uv run pytest tests/test_ai_user_id.py tests/test_streaming_finalize.py -q -n 0 --no-cov -k "run_completed or final_text or hidden_tool_only or terminal_send_retry"
```

Expected:
- FAIL on stale text, retry, or placeholder-success behavior

- [x] **Step 3: Implement the shared canonicalization step and wire every consumer to it.**

Requirements:
- one authoritative final assistant-text rule
- one authoritative rendered visible-body rule
- no independent tool-marker reconstruction

- [x] **Step 4: Re-run the targeted text/stream tests until green.**

Run:
```bash
uv run pytest tests/test_ai_user_id.py tests/test_streaming_finalize.py -q -n 0 --no-cov -k "run_completed or final_text or hidden_tool_only or terminal_send_retry"
```

Expected:
- PASS

- [x] **Step 5: Commit the shared final-text boundary.**

```bash
git add src/mindroom/streaming.py src/mindroom/ai.py src/mindroom/teams.py src/mindroom/api/openai_compat.py tests/test_ai_user_id.py tests/test_streaming_finalize.py
git commit -m "refactor: unify terminal final text authority"
```

## Task 5: Migrate Post-Response Effects To Policy-Driven Behavior

**Files:**
- Modify: `src/mindroom/post_response_effects.py`
- Modify: `src/mindroom/final_delivery.py`
- Test: `tests/test_queued_message_notify.py`
- Test: `tests/test_final_delivery.py`

- [x] **Step 1: Write failing post-effect tests for policy-driven behavior.**

Cover at minimum:
- persistence and thread summary use `response_identity_event_id`
- late-failure shielding uses `turn_completion_event_id`
- `suppression_cleanup_failed` shields but does not persist
- preserved-stream interactive replies register follow-up
- cancellation-derived states do not register interactive follow-up
- suppressed compaction dispatch honors the policy row

- [x] **Step 2: Run the targeted post-effect tests and confirm failure.**

Run:
```bash
uv run pytest tests/test_queued_message_notify.py tests/test_final_delivery.py -q -n 0 --no-cov -k "response_identity or turn_completion or suppression_cleanup_failed or interactive"
```

Expected:
- FAIL on raw `DeliveryResult` gating

- [x] **Step 3: Refactor `post_response_effects.py` to consume policy-derived fields only.**

Requirements:
- no success inference from `DeliveryResult.event_id`
- interactive registration keys off policy-driven response identity
- compaction suppression path is explicit

- [x] **Step 4: Re-run the targeted post-effect tests until green.**

Run:
```bash
uv run pytest tests/test_queued_message_notify.py tests/test_final_delivery.py -q -n 0 --no-cov -k "response_identity or turn_completion or suppression_cleanup_failed or interactive"
```

Expected:
- PASS

- [x] **Step 5: Commit policy-driven post-effects.**

```bash
git add src/mindroom/post_response_effects.py src/mindroom/final_delivery.py tests/test_queued_message_notify.py tests/test_final_delivery.py
git commit -m "refactor: drive post-response effects from terminal policy"
```

## Task 6: Migrate Callers And Outward-Facing APIs

**Files:**
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/edit_regenerator.py`
- Modify: `src/mindroom/commands/handler.py`
- Test: `tests/test_multi_agent_bot.py`
- Test: `tests/test_cancelled_response_hook.py`

- [x] **Step 1: Write failing caller-boundary tests.**

Cover at minimum:
- normal responses stop using `str | None` as control flow
- skill-command paths stop using raw event ids as success
- cancelled-with-visible-note marks handled only according to policy
- cancelled regeneration remains retryable if policy says it should
- hard-cancelled visible placeholder preserves visible targeting without promoting response identity

- [x] **Step 2: Run the targeted caller tests and confirm failure.**

Run:
```bash
uv run pytest tests/test_multi_agent_bot.py tests/test_cancelled_response_hook.py -q -n 0 --no-cov -k "skill_command or handled or retryable or hard_cancel"
```

Expected:
- FAIL on caller-side truthiness or wrong handledness

- [x] **Step 3: Refactor outward-facing response APIs to return typed results internally.**

Requirements:
- `ResponseRunner.generate_response()` and `send_skill_command_response()` migrate first
- wrappers either return typed results or become explicit leaf projections
- no wrapper is used for control flow if it still returns an event id

- [x] **Step 4: Update `bot.py`, `turn_controller.py`, `edit_regenerator.py`, and `commands/handler.py` to use explicit fields.**

- [x] **Step 5: Re-run the targeted caller tests until green.**

Run:
```bash
uv run pytest tests/test_multi_agent_bot.py tests/test_cancelled_response_hook.py -q -n 0 --no-cov -k "skill_command or handled or retryable or hard_cancel"
```

Expected:
- PASS

- [x] **Step 6: Run the stop-the-line caller grep.**

Run:
```bash
rg -n "-> str \\| None|Awaitable\\[str \\| None\\]" src/mindroom/bot.py src/mindroom/turn_controller.py src/mindroom/edit_regenerator.py src/mindroom/commands/handler.py src/mindroom/response_runner.py
rg -n "event_id is not None|response_event_id is not None|raw_event_id" src/mindroom/bot.py src/mindroom/turn_controller.py src/mindroom/edit_regenerator.py src/mindroom/commands/handler.py
```

Expected:
- no control-flow-facing `str | None` boundaries remain
- any surviving event-id wrappers are leaf projections only

- [x] **Step 7: Commit the caller migration.**

```bash
git add src/mindroom/response_runner.py src/mindroom/bot.py src/mindroom/turn_controller.py src/mindroom/edit_regenerator.py src/mindroom/commands/handler.py tests/test_multi_agent_bot.py tests/test_cancelled_response_hook.py
git commit -m "refactor: migrate callers to typed terminal delivery results"
```

## Task 7: Delete Legacy Reconstruction And Run End-To-End Verification

**Files:**
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/response_lifecycle.py`
- Modify: `src/mindroom/post_response_effects.py`
- Test: existing targeted suites

- [x] **Step 1: Write one failing regression test for each remaining legacy helper you plan to remove.**

Focus on:
- `resolve_response_event_id`
- `_is_cancelled_delivery_result`
- any remaining `tracked_event_id` semantic fallback

- [x] **Step 2: Remove the legacy reconstruction helpers and replace remaining uses with canonical fields.**

- [x] **Step 3: Re-run focused suites.**

Run:
```bash
uv run pytest tests/test_final_delivery.py tests/test_streaming_finalize.py tests/test_cancelled_response_hook.py tests/test_queued_message_notify.py tests/test_ai_user_id.py -q -n 0 --no-cov
uv run pytest tests/test_multi_agent_bot.py -q -n 0 --no-cov -k "skill_command or handled or retryable or hard_cancel or suppression_cleanup_failed"
```

Expected:
- PASS

- [x] **Step 4: Run the final contract grep suite.**

Run:
```bash
rg -n "resolve_response_event_id|_is_cancelled_delivery_result" src/mindroom
rg -n "emit_cancelled_response|emit_after_response" src/mindroom/response_runner.py src/mindroom/response_lifecycle.py
rg -n "-> str \\| None|Awaitable\\[str \\| None\\]" src/mindroom/bot.py src/mindroom/turn_controller.py src/mindroom/edit_regenerator.py src/mindroom/commands/handler.py src/mindroom/response_runner.py
```

Expected:
- no hits, or only transport-only helpers with no semantic role and a comment proving why they remain

- [x] **Step 5: Run pre-commit and full targeted verification.**

Run:
```bash
uv run pre-commit run --all-files
```

Expected:
- PASS

- [x] **Step 6: Commit the legacy seam removal.**

```bash
git add src/mindroom/response_runner.py src/mindroom/response_lifecycle.py src/mindroom/post_response_effects.py tests/test_final_delivery.py tests/test_streaming_finalize.py tests/test_cancelled_response_hook.py tests/test_queued_message_notify.py tests/test_ai_user_id.py tests/test_multi_agent_bot.py
git commit -m "refactor: remove legacy terminal delivery reconstruction"
```
