# Terminal Delivery Contract Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the remaining legacy delivery bridges so terminal delivery has one source of truth end-to-end and no fallback path reinterprets semantics from partial facts.

**Architecture:** The closure pass is deletion-oriented. Each phase removes one class of bridge, replaces it with policy-driven typed behavior, and adds tests plus grep gates that make the old pattern impossible to reintroduce quietly.

**Tech Stack:** Python 3.12, asyncio, dataclasses, Matrix nio integration, pytest, existing terminal-delivery policy table and lifecycle abstractions.

---

## Accountability Protocol

This plan is a merge gate, not a suggestion.
Any unchecked item means the migration is still incomplete.

### Hard Rules

- [ ] No semantic bridge is preserved for convenience.
- [ ] No stale review item is implemented without reproducing it on current `HEAD`.
- [ ] No phase is complete until one old seam is actually deleted.
- [ ] No runtime code outside the canonical projection helper constructs `TurnDeliveryResolution(...)`.
- [ ] No runtime code outside `delivery_gateway.py` emits terminal hooks.
- [ ] No outward-facing response lifecycle control-flow API returns `str | None`.

### Closure Grep Gates

These are failing checks, not advisory checks.
If any command returns a live runtime hit, stop and fix that phase before continuing.

```bash
rg -n "resolve_response_event_id\\(|_coerce_final_delivery_outcome|_is_cancelled_delivery_result" src/mindroom
rg -n "emit_cancelled_response|emit_after_response" src/mindroom | rg -v "delivery_gateway.py"
rg -n "TurnDeliveryResolution\\(" src/mindroom | rg -v "final_delivery.py"
rg -n "-> str \\| None|Awaitable\\[str \\| None\\]" src/mindroom/{response_lifecycle.py,response_runner.py,bot.py,turn_controller.py,edit_regenerator.py,commands/handler.py}
rg -n "event_id is not None|response_event_id is not None" src/mindroom/{response_runner.py,bot.py,turn_controller.py,edit_regenerator.py,commands/handler.py}
rg -n "SuppressedPlaceholderCleanupError" src/mindroom
```

## Phase -1: Inventory Current Bridges Before Editing

**Files:**
- Inspect: `src/mindroom/final_delivery.py`
- Inspect: `src/mindroom/delivery_gateway.py`
- Inspect: `src/mindroom/response_lifecycle.py`
- Inspect: `src/mindroom/response_runner.py`
- Inspect: `src/mindroom/post_response_effects.py`
- Inspect: `src/mindroom/streaming.py`
- Inspect: `src/mindroom/ai.py`
- Inspect: `src/mindroom/api/openai_compat.py`
- Inspect: `src/mindroom/bot.py`
- Inspect: `src/mindroom/turn_controller.py`
- Inspect: `src/mindroom/edit_regenerator.py`
- Inspect: `src/mindroom/commands/handler.py`
- Inspect: `src/mindroom/teams.py`

- [ ] Build a bridge inventory from current `HEAD`, not from stale review comments.
- [ ] For each live bridge, record:
  - file
  - exact function
  - why it is a semantic bridge
  - owner phase below
  - failing test that will prove removal
- [ ] Mark every stale review item as stale and do not implement it.
- [ ] Run the closure grep gates once before editing and save the output in working notes.

## Phase 0: Ground Rules

- [ ] No production code change without a failing test first.
- [ ] No compatibility wrapper that preserves `str | None` control flow.
- [ ] No terminal hook emission outside `delivery_gateway.py`.
- [ ] No exception path after delivery starts that bypasses `FinalDeliveryOutcome`.
- [ ] No branch is mergeable until the post-migration audit checklist passes in full.

## Phase 1: Delete Caller-Facing `str | None` Semantics Completely

**Files:**
- Modify: `src/mindroom/response_lifecycle.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/edit_regenerator.py`
- Modify: `src/mindroom/commands/handler.py`
- Test: `tests/test_ai_user_id.py`
- Test: `tests/test_edit_response_regeneration.py`
- Test: `tests/test_multi_agent_bot.py`

- [ ] Write failing tests that prove cancellation-derived states do not persist `turn_completion_event_id` as `response_event_id`.
- [ ] Write failing tests that prove all outward-facing response APIs return `TurnDeliveryResolution`.
- [ ] Remove any remaining caller-facing `str | None` control-flow wrappers.
- [ ] Remove or inline any single-event-id reconstruction helpers still used for semantics.
- [ ] Update callers to use `response_identity_event_id` for persistence and regeneration.
- [ ] Update callers to use `should_mark_handled` for handled-turn decisions.
- [ ] Update any docs or type hints that still describe the response lifecycle result as a message-event-id string.
- [ ] Run focused tests for edit regeneration, handled-turn tracking, and cancelled visible-note paths.
- [ ] Run grep audit:
  ```bash
  rg -n "-> str \\| None|Awaitable\\[str \\| None\\]" src/mindroom/{response_lifecycle.py,response_runner.py,bot.py,turn_controller.py,edit_regenerator.py,commands/handler.py}
  rg -n "resolve_response_event_id\\(" src/mindroom
  ```
- [ ] Commit phase:
  ```bash
  git add src/mindroom/response_lifecycle.py src/mindroom/response_runner.py src/mindroom/bot.py src/mindroom/turn_controller.py src/mindroom/edit_regenerator.py src/mindroom/commands/handler.py tests/test_ai_user_id.py tests/test_edit_response_regeneration.py tests/test_multi_agent_bot.py
  git commit -m "refactor: remove caller-facing response id contract"
  ```

## Phase 2: Eliminate Runtime Semantic Recovery Bridges

**Files:**
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Test: `tests/test_streaming_finalize.py`
- Test: `tests/test_cancelled_response_hook.py`
- Test: `tests/test_multi_agent_bot.py`

- [ ] Write failing tests that prove late streamed-finalization exceptions preserve already-visible output without `_coerce_final_delivery_outcome()` semantics.
- [ ] Write failing tests that prove missing-delivery fallback does not reconstruct success/failure from placeholder ids or tracked ids.
- [ ] Refactor runtime fallback paths so only raw transport facts cross the failure boundary.
- [ ] Delete `_coerce_final_delivery_outcome()` from migrated execution paths.
- [ ] Delete any remaining runtime use of `DeliveryResult` to infer terminal semantics.
- [ ] Delete any remaining semantic projector that tries to rebuild a single logical event id from canonical outcomes for control flow.
- [ ] Run focused streaming-finalization and late-exception tests.
- [ ] Run grep audit:
  ```bash
  rg -n "_coerce_final_delivery_outcome|DeliveryResult\\(|resolve_response_event_id\\(" src/mindroom/response_runner.py src/mindroom/delivery_gateway.py
  ```
- [ ] Commit phase:
  ```bash
  git add src/mindroom/response_runner.py src/mindroom/delivery_gateway.py tests/test_streaming_finalize.py tests/test_cancelled_response_hook.py tests/test_multi_agent_bot.py
  git commit -m "refactor: remove delivery semantic recovery bridges"
  ```

## Phase 3: Close Terminal Hook Ownership Fully

**Files:**
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/response_runner.py`
- Test: `tests/test_cancelled_response_hook.py`
- Test: `tests/test_multi_agent_bot.py`

- [ ] Write failing tests for non-streaming failed send/edit paths proving `message:cancelled` fires exactly once.
- [ ] Write failing tests proving suppression cleanup does not emit cancellation hooks before cleanup result is known.
- [ ] Ensure every non-success final-delivery outcome flows through gateway-owned terminal hook emission exactly once.
- [ ] Ensure suppression cleanup failure remains a canonical outcome, not exception control flow.
- [ ] Remove any remaining runner-owned terminal hook backfill.
- [ ] Prove with tests that cleanup-dependent hook emission happens only after cleanup outcome is known.
- [ ] Run focused hook tests.
- [ ] Run grep audit:
  ```bash
  rg -n "emit_cancelled_response|emit_after_response" src/mindroom/response_runner.py src/mindroom/response_lifecycle.py src/mindroom/turn_controller.py
  ```
- [ ] Commit phase:
  ```bash
  git add src/mindroom/delivery_gateway.py src/mindroom/response_runner.py tests/test_cancelled_response_hook.py tests/test_multi_agent_bot.py
  git commit -m "refactor: close terminal hook ownership in gateway"
  ```

## Phase 4: Preserve Visible Streams Both Physically And Interactively

**Files:**
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/post_response_effects.py`
- Test: `tests/test_streaming_finalize.py`
- Test: `tests/test_cancelled_response_hook.py`
- Test: `tests/test_multi_agent_bot.py`

- [ ] Write failing tests proving failed streamed re-edits do not redact the already-visible stream.
- [ ] Write failing tests proving preserved-stream failure outcomes retain `option_map` and `options_list`.
- [ ] Write failing tests proving preserved-stream failure outcomes still register interactive follow-up when policy allows.
- [ ] Fix preserved-stream re-edit paths so `existing_event_is_placeholder` is never used for already-visible streams.
- [ ] Fix preserved-stream failure builders so interactive metadata survives.
- [ ] Fix post-response effects so interactive registration follows policy, not `has_final_visible_delivery` shortcuts.
- [ ] Prove with tests that a preserved visible stream remains physically present after failed re-edit fallback.
- [ ] Run focused preserved-stream tests.
- [ ] Commit phase:
  ```bash
  git add src/mindroom/delivery_gateway.py src/mindroom/post_response_effects.py tests/test_streaming_finalize.py tests/test_cancelled_response_hook.py tests/test_multi_agent_bot.py
  git commit -m "fix: preserve visible stream state on terminal fallback"
  ```

## Phase 5: Unify Final Assistant Text Authority

**Files:**
- Modify: `src/mindroom/ai.py`
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/api/openai_compat.py`
- Modify: `src/mindroom/teams.py`
- Test: `tests/test_ai_user_id.py`
- Test: `tests/test_openai_compat.py`

- [ ] Write failing tests for partial-then-corrected `RunCompletedEvent.content` persistence in recorder and replay state.
- [ ] Write failing tests for final empty-string overwrite semantics.
- [ ] Write failing tests for the chosen OpenAI SSE projection rule, including multi-tail correction boundaries if supported.
- [ ] Make one canonical final-assistant-text helper and route recorder/replay consumers through it.
- [ ] Keep SSE behavior explicit and documented if transport limitations prevent full rewrite semantics.
- [ ] Document the exact SSE projection contract in code comments or tests so adapter limits do not become another implicit bridge.
- [ ] Run focused final-text tests.
- [ ] Commit phase:
  ```bash
  git add src/mindroom/ai.py src/mindroom/streaming.py src/mindroom/api/openai_compat.py src/mindroom/teams.py tests/test_ai_user_id.py tests/test_openai_compat.py
  git commit -m "refactor: unify authoritative final assistant text"
  ```

## Phase 6: Final Closure Audit

**Files:**
- Modify only if an audit check fails

- [ ] Run full suite:
  ```bash
  uv run pytest -q -n auto --no-cov
  ```

- [ ] Run focused hook/delivery suites again:
  ```bash
  uv run pytest tests/test_final_delivery.py tests/test_streaming_finalize.py tests/test_cancelled_response_hook.py tests/test_ai_user_id.py tests/test_openai_compat.py tests/test_multi_agent_bot.py -q -n auto --no-cov
  ```

- [ ] Run pre-commit on touched files:
  ```bash
  uv run pre-commit run --files <all touched files>
  ```

- [ ] Run closure grep checklist:
  ```bash
  rg -n "resolve_response_event_id\\(|_coerce_final_delivery_outcome|DeliveryResult\\(" src/mindroom
  rg -n "-> str \\| None|Awaitable\\[str \\| None\\]" src/mindroom/{response_lifecycle.py,response_runner.py,bot.py,turn_controller.py,edit_regenerator.py,commands/handler.py}
  rg -n "emit_cancelled_response|emit_after_response" src/mindroom/response_runner.py src/mindroom/response_lifecycle.py src/mindroom/turn_controller.py
  rg -n "SuppressedPlaceholderCleanupError" src/mindroom
  rg -n "response_event_id is not None|event_id is not None" src/mindroom/{bot.py,turn_controller.py,edit_regenerator.py,commands/handler.py}
  rg -n "TurnDeliveryResolution\\(" src/mindroom | rg -v "final_delivery.py"
  ```

- [ ] If any grep returns a live runtime path, stop and fix it before claiming completion.

- [ ] Review every current review comment against final `HEAD` and sort it into exactly one bucket:
  - reproduced and fixed with a test
  - stale against current `HEAD`
  - explicit transport limitation documented by policy

- [ ] Do not merge until every current review item is in one of those buckets.

## Post-Migration Audit Checklist

This checklist is the merge gate.
Every item must be `yes`.

### A. Canonical State Ownership

- [ ] Every terminal state comes from `FinalDeliveryOutcome`.
- [ ] `TurnDeliveryResolution` is only a projection, not a second semantic model.
- [ ] No runtime semantic recovery helper remains.
- [ ] No runtime file outside `final_delivery.py` constructs `TurnDeliveryResolution(...)` directly.

### B. Caller Boundary

- [ ] No outward-facing response lifecycle API returns `str | None`.
- [ ] No caller persists `turn_completion_event_id` as `response_event_id`.
- [ ] No caller uses event-id truthiness to decide handledness or success.
- [ ] No docs or type hints still describe the lifecycle result as a bare response event id.

### C. Hook Ownership

- [ ] All terminal hooks are emitted in `delivery_gateway.py`.
- [ ] Non-streaming failed send/edit emits `message:cancelled` exactly once.
- [ ] Suppression cleanup does not emit cancellation hooks before cleanup result is known.
- [ ] No runner/controller code backfills hook emission for gateway outcomes.

### D. Exception Safety

- [ ] No delivery-stage exception bypasses `FinalDeliveryOutcome`.
- [ ] `suppression_cleanup_failed` stays typed and retryable.
- [ ] Late streamed-finalization failures preserve already-visible output when policy says they should.
- [ ] No cleanup path raises semantic exceptions once delivery coordination has started.

### E. Interactive Integrity

- [ ] Preserved visible streamed replies keep `option_map`.
- [ ] Preserved visible streamed replies keep `options_list`.
- [ ] Interactive follow-up registration follows policy, not success-only shortcuts.
- [ ] A preserved interactive stream remains answerable after fallback, not just visible.

### F. Final Text Integrity

- [ ] Recorder persistence uses canonical final assistant text.
- [ ] Interrupted replay persistence uses canonical final assistant text.
- [ ] Matrix final delivery uses canonical final assistant text.
- [ ] OpenAI-compatible SSE follows one explicit tested projection rule.
- [ ] SSE projection limits are explicit and documented, not accidental.

### G. Physical Invariants

- [ ] No preserved-stream outcome redacts the event it claims to preserve.
- [ ] No placeholder-only artifact is promoted into a durable response identity.
- [ ] No visible-placeholder artifact is treated like a durable handled response unless policy explicitly says so.

### H. Verification

- [ ] Full suite passes with `-n auto`.
- [ ] Focused terminal-delivery suites pass with `-n auto`.
- [ ] `pre-commit` passes on all touched files.
- [ ] Grep closure checks return no live violations.

### I. Review Closure

- [ ] Every live review finding is reproduced or disproved on current `HEAD`.
- [ ] Every reproduced finding has a targeted regression test.
- [ ] Every stale finding is consciously ignored rather than patched "just in case."

## Final Rule

If even one checklist item fails, the migration is not finished.
