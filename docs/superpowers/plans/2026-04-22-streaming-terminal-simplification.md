# Streaming Terminal Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify streaming finalization so visible streamed replies can only receive terminal status updates plus one optional end-only final transform on clean success.

**Architecture:** Keep `message:before_response` as a pre-visible draft hook, add one explicit text-only `message:final_response_transform` hook for best-effort end-only rewrites on clean success, and remove late suppression/full-rewrite/redaction behavior once real visible streamed text has landed. Preserve placeholder cleanup, terminal-note updates, and non-mutating terminal hooks, carry a separate canonical final-body candidate alongside the committed visible body, and keep preserved-visible fallback states only for real terminal-note / late-edit failures instead of deleting them.

**Tech Stack:** Python 3.12, pytest, existing hook framework, Matrix streaming delivery pipeline

### Current Merged-Branch Constraints

- This plan is reviewed against the current merged branch tip after `95114dda5`.
- Explicit cancellation provenance now lives in:
  - `src/mindroom/cancellation.py`
  - `src/mindroom/orchestration/runtime.py`
- Runtime code now treats cancellation provenance as one of:
  - `user_stop`
  - `sync_restart`
  - `interrupted`
- `delivery_gateway.py` may not import `mindroom.orchestration.runtime` directly without violating Tach boundaries.
- Task 4 must therefore **not** deduplicate cancellation handling by introducing a forbidden `delivery_gateway.py -> orchestration.runtime` dependency.
- The real remaining duplication is the `CancelSource -> failure_reason` mapping in:
  - `src/mindroom/response_runner.py` via `_cancel_failure_reason()`
  - `src/mindroom/delivery_gateway.py` via `_cancelled_note_update()`
- `_cancelled_response_update` is already gone on the current tip and is no longer a cleanup target.

---

## File Map

- Modify: `src/mindroom/hooks/types.py`
  - Register the new built-in final-transform event and its timeout.
- Modify: `src/mindroom/hooks/context.py`
  - Add a dedicated text-only context/draft type for one-shot final transforms without suppression/deletion semantics.
- Modify: `src/mindroom/hooks/execution.py`
  - Add a best-effort wrapper around the shared serial transform path for the new hook event.
- Modify: `src/mindroom/hooks/__init__.py`
  - Export the new hook event and context from the public hook package surface.
- Modify: `src/mindroom/delivery_gateway.py`
  - Enforce the simplified post-visible streaming rules.
  - Remove streamed post-visible suppression/full rewrite via `message:before_response`.
  - Run the new final transform only on clean successful completion.
- Modify: `src/mindroom/final_delivery.py`
  - Carry both committed visible body facts and a separate canonical final-body candidate through the streamed transport boundary.
  - Keep the preserved-visible fallback states that the simplified product rules still need.
  - Remove or de-emphasize only states that become unreachable after streamed post-visible mutation is gone.
- Modify: `src/mindroom/streaming.py`
  - Keep terminal note updates and placeholder cleanup.
  - Remove slow retry/backoff from cancel/error terminal updates while the lifecycle is blocked.
- Modify: `src/mindroom/streaming_delivery.py`
  - Stop incrementally rewriting visible streamed text from canonical completion content.
  - Preserve canonical final content only as a clean-success final-body candidate.
- Modify: `src/mindroom/response_runner.py`
  - Remove duplicate cancellation/delivery helpers made obsolete by the simplified boundary.
  - Ensure early exits still emit terminal hooks.
- Modify: `src/mindroom/bot.py`
  - Route team empty-prompt exits through canonical terminal hook emission.
- Modify: `docs/hooks.md`
  - Document the simplified hook contract in plain English.
- Test: `tests/test_hook_execution.py`
  - Add unit tests for the new final transform hook behavior.
- Test: `tests/test_cancelled_response_hook.py`
  - Verify terminal hooks fire under the simplified rules.
- Test: `tests/test_streaming_behavior.py`
  - Verify visible streamed replies only receive terminal-note updates after visibility.
- Test: `tests/test_streaming_finalize.py`
  - Verify placeholder cleanup and cancel/error finalize behavior.
- Verify: `tests/test_sync_task_cancellation.py`
  - Keep shared cancellation provenance behavior stable while Task 4 narrows terminal delivery cleanup.
- Verify: `tests/test_ai_error_message_display.py`
  - Keep user-stop vs restart vs generic interruption messaging stable.
- Verify: `tests/test_team_scheduler_context.py`
  - Keep generic interruption and restart-note behavior stable on team paths.
- Test: `tests/test_multi_agent_bot.py`
  - Verify streaming/regeneration behavior with existing visible replies.
- Test: `tests/test_final_delivery.py`
  - Update the canonical terminal-state policy matrix for the retained state set.
- Test: `tests/test_ai_user_id.py`
  - Verify terminal hook emission and late-failure resolution behavior remain canonical on the helper paths.

### Desired Rules

- `message:before_response` is a pre-send draft hook.
- `message:before_response` may mutate or suppress only before the first real visible response text lands.
- After real visible response text lands, streamed content may not be suppressed, deleted, or fully rewritten by that hook.
- After real visible response text lands, the only automatic mutation allowed on cancel/error/restart is terminal status decoration.
- On clean success, one optional text-only `message:final_response_transform` hook may replace the final visible text once.
- `message:final_response_transform` may not suppress, redact, delete, or mutate metadata for the reply.
- If `message:final_response_transform` times out, raises, or is cancelled, keep the already-visible streamed text unchanged.
- If the final transform or its edit fails, keep the already-visible streamed text and still resolve as clean success with `message:after_response`.
- If no real visible text ever lands, placeholder cleanup/replacement is still allowed.
- If provider canonical final content arrives after visible text lands, it may only be used as the clean-success final-body candidate for one final replacement edit.
- Provider canonical final content must not be incrementally merged into or reorder already-visible streamed text.
- If a final transform changes the visible text, recompute interactive follow-up options from the transformed final text before persisting the clean success outcome.
- `message:cancelled` means every terminal outcome other than clean success, including suppression/cleanup outcomes that do not complete as a clean visible response.

### Non-Goals

- No mid-stream token-by-token moderation or transformation.
- No retroactive suppression of already-visible streamed text.
- No retry semantics that depend on whether a best-effort final transform succeeded.
- No mid-stream rewriting of visible content to reconcile provider canonical final text.

### State Scope

- Keep the preserved-visible-stream states used when a late terminal-note edit or other real delivery failure happens after text is already visible.
- Keep the existing visible-response states that non-streaming edit/regeneration flows still use.
- Keep `suppression_cleanup_failed` only for explicit cleanup paths that still exist after the simplification.
- Treat best-effort final-transform failure as clean success with the already-visible body, not as a preserved-visible failure state.
- Remove or stop using only the streamed post-visible suppression/rewrite states that become unreachable after `message:before_response` is no longer allowed to mutate visible streamed text.

### Task 1: Introduce a Dedicated Final Response Transform Hook

**Files:**
- Modify: `src/mindroom/hooks/types.py`
- Modify: `src/mindroom/hooks/context.py`
- Modify: `src/mindroom/hooks/execution.py`
- Modify: `src/mindroom/hooks/__init__.py`
- Modify: `tests/test_hook_execution.py`

- [ ] **Step 1: Write a failing unit test for the new built-in hook event**

Add a unit test proving `message:final_response_transform` is rejected until it is registered as a built-in hook event.

- [ ] **Step 2: Run the single hook-surface test to verify it fails**

Run: `uv run pytest tests/test_hook_execution.py -q -n 0 --no-cov -k 'final_response_transform_builtin_event'`
Expected: FAIL because the new reserved `message:` event is not yet registered.

- [ ] **Step 3: Register and export the new hook event**

Add `EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM` to:
- `BUILTIN_EVENT_NAMES`
- `DEFAULT_EVENT_TIMEOUT_MS`
- the public exports in `src/mindroom/hooks/__init__.py`

Set the default timeout explicitly to `200ms` so the hook stays in the same latency class as `message:before_response`.

- [ ] **Step 4: Add a dedicated text-only final-transform context**

Add a context/draft type in `src/mindroom/hooks/context.py` that carries:
- `response_text`
- `response_kind`
- `envelope`

Do **not** include `suppress`, `tool_trace`, or `extra_content`.
Export the new context/draft types from `src/mindroom/hooks/__init__.py`.
Update the hook-context helpers that currently hard-code response-context types so requester propagation and synthetic hook depth work for the new context too.

- [ ] **Step 5: Add a best-effort final-transform wrapper on top of the shared serial transform machinery**

Use the existing serial transform machinery, but do **not** silently change `message:before_response` semantics.
The new hook needs:
- copy-on-write draft isolation so mutation + raise/cancel cannot leak partial changes
- best-effort timeout/exception/cancellation handling that preserves the prior draft
- unchanged `message:before_response` cancellation propagation on the pre-send path
- agent/room scope filtering wired for the new context type

- [ ] **Step 6: Add unit tests for the new hook contract**

Add tests showing:
- a hook may return replacement final text
- hooks run serially and see the latest transformed draft
- suppression is unavailable on this hook
- timeout, cancellation, or exception leaves the draft unchanged
- agent/room scoping still works for the new context
- hook-originated send/requester/depth propagation still works for the new context

- [ ] **Step 7: Run the focused hook tests**

Run: `uv run pytest tests/test_hook_execution.py -q -n 0 --no-cov -k 'final_response_transform'`
Expected: PASS

- [ ] **Step 8: Commit the hook surface**

```bash
git add src/mindroom/hooks/types.py src/mindroom/hooks/context.py src/mindroom/hooks/execution.py src/mindroom/hooks/__init__.py tests/test_hook_execution.py
git commit -m "feat: add final response transform hook"
```

### Task 2: Lock Down the Simplified Product Rules in Tests

**Files:**
- Modify: `tests/test_streaming_behavior.py`
- Modify: `tests/test_streaming_finalize.py`
- Modify: `tests/test_multi_agent_bot.py`
- Modify: `tests/test_cancelled_response_hook.py`

- [ ] **Step 1: Replace the now-wrong opposite-behavior streamed tests with failing regressions for the new rules**

Replace the current tests that encode post-visible streamed suppression/rewrite behavior with failing regressions for the simplified contract.
This includes the current opposite-behavior cases in:
- `tests/test_multi_agent_bot.py`
- `tests/test_cancelled_response_hook.py`

- [ ] **Step 2: Add a failing regression that `message:before_response` no longer mutates post-visible streamed success**

Add a regression that streams real visible text, installs a `message:before_response` hook that tries to rewrite or suppress at the streamed-finalize point, and assert the visible reply remains unchanged.

- [ ] **Step 3: Run the single `before_response` regression to verify it fails**

Run: `uv run pytest tests/test_multi_agent_bot.py -q -n 0 --no-cov -k 'streamed_before_response_no_longer_mutates_post_visible_success'`
Expected: FAIL because the current gateway still mutates visible streamed success through `message:before_response`.

- [ ] **Step 4: Add a failing regression that canonical final content is not incrementally merged into visible streamed text**

Add a regression that streams visible text, then receives `RunCompletedEvent.content` that is not a safe suffix-extension, and assert the visible streamed body is left untouched during streaming.
Update the existing merge-focused tests in `tests/test_streaming_finalize.py` instead of leaving the old behavior pinned.

- [ ] **Step 5: Run the canonical-final-content regression to verify it fails**

Run: `uv run pytest tests/test_streaming_finalize.py -q -n 0 --no-cov -k 'run_completed_content_does_not_rewrite_visible_stream_text'`
Expected: FAIL because the current streaming path still rewrites visible text from `RunCompletedEvent.content`.

- [ ] **Step 6: Add a failing regression that clean streamed success may be rewritten once through the new hook**

Add a regression that streams visible text, applies one `message:final_response_transform`, and asserts the final visible event is updated once.

- [ ] **Step 7: Run the single final-transform regression to verify the product rule is not implemented yet**

Run: `uv run pytest tests/test_streaming_behavior.py -q -n 0 --no-cov -k 'streamed_success_allows_one_final_response_transform'`
Expected: FAIL because the current success rewrite still comes from the wrong hook path.

- [ ] **Step 8: Add a failing regression that final-transform failure leaves visible streamed text intact and still resolves as clean success**

Add a regression where the final-transform hook returns replacement text but the final edit fails, and assert the already-visible streamed text remains visible, `message:after_response` still fires, and the result is not reclassified as cancelled/error.

- [ ] **Step 9: Run the failure-preservation regression to verify it fails for the right reason**

Run: `uv run pytest tests/test_streaming_finalize.py -q -n 0 --no-cov -k 'final_response_transform_failure_keeps_visible_stream_text'`
Expected: FAIL because the current logic still treats this as a broader post-stream rewrite path.

- [ ] **Step 10: Add a failing regression that streamed regeneration against an existing visible reply preserves linkage when no new body lands**

Add a regression for the current `existing_event_is_placeholder=False` path so a streamed regeneration that never produces new visible body text still preserves the existing visible response linkage instead of collapsing to `*_without_visible_response`.

- [ ] **Step 11: Commit the failing-test baseline**

```bash
git add tests/test_streaming_behavior.py tests/test_streaming_finalize.py tests/test_multi_agent_bot.py tests/test_cancelled_response_hook.py
git commit -m "test: lock down simplified streamed finalization rules"
```

### Task 3: Narrow Streaming Finalization to Terminal Notes Plus One Final Transform

**Files:**
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/final_delivery.py`
- Modify: `tests/test_streaming_finalize.py`
- Modify: `tests/test_streaming_behavior.py`
- Modify: `tests/test_multi_agent_bot.py`
- Modify: `tests/test_final_delivery.py`
- Modify: `tests/test_ai_user_id.py`

- [ ] **Step 1: Stop running `message:before_response` as a post-visible streamed mutation hook**

In `finalize_streamed_response()`, remove the current post-visible `message:before_response` suppression/full-rewrite behavior once `visible_stream_event_id` exists.

- [ ] **Step 2: Keep placeholder-only cleanup as the only cleanup path before visibility**

Preserve the current placeholder-only cleanup path so `Thinking...` can still be removed/replaced cleanly when no real visible text landed.

- [ ] **Step 3: Add the clean-success final-transform path**

On clean successful streamed completion:
- build a final-transform draft from `canonical_final_body_candidate` when present, otherwise from the completed visible text
- run `message:final_response_transform`
- if the draft is unchanged, do nothing
- if changed, recompute interactive options from the transformed final text and attempt exactly one final edit
- if the edit fails, keep the visible streamed text and still resolve as clean success
- if the hook times out, raises, or is cancelled, keep the visible streamed text and still resolve as clean success

- [ ] **Step 4: Remove streamed post-visible suppression/redaction**

If a plugin wants to “suppress” after visible text exists, that is no longer supported here.
Return the visible response as-is and let terminal hooks/logging carry the outcome.

- [ ] **Step 5: Preserve existing visible-reply linkage for streamed regeneration before first new token**

When streaming against an existing non-placeholder visible reply, keep that visible-response linkage if the new stream never lands any new visible body text.
Do not collapse that path to `*_without_visible_response`.

- [ ] **Step 6: Make the retained state set explicit**

Keep:
- the preserved-visible-stream states for failed terminal-note updates or other real late delivery failures after visible text
- the non-streaming existing-visible-response states still used by regeneration/edit flows
- the streamed existing-visible-response linkage for in-place regeneration failures before first new token
- placeholder-only cleanup and hidden cancel/error states
- `suppression_cleanup_failed` only for the explicit cleanup paths that remain after streamed post-visible suppression is gone

Remove or stop using only the streamed post-visible suppression/rewrite states that become unreachable after the new rules land.
Do **not** classify best-effort final-transform failure as a cancelled/error terminal state.

- [ ] **Step 7: Update the canonical policy tests**

Revise `tests/test_final_delivery.py` and `tests/test_ai_user_id.py` so the retained state set is explicit and the simplified streamed-finalization contract is asserted exhaustively.

- [ ] **Step 8: Run the focused streaming and policy tests**

Run: `uv run pytest tests/test_streaming_behavior.py tests/test_streaming_finalize.py tests/test_multi_agent_bot.py tests/test_final_delivery.py tests/test_ai_user_id.py -q -n auto --no-cov`
Expected: PASS

- [ ] **Step 9: Commit the narrowed gateway behavior**

```bash
git add src/mindroom/delivery_gateway.py src/mindroom/final_delivery.py tests/test_streaming_finalize.py tests/test_streaming_behavior.py tests/test_multi_agent_bot.py tests/test_final_delivery.py tests/test_ai_user_id.py
git commit -m "refactor: narrow streamed finalization mutations"
```

### Task 4: Simplify Terminal Delivery Reliability and Delete Duplicate Logic

**Files:**
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/streaming_delivery.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/bot.py`
- Modify: `tests/test_streaming_finalize.py`
- Modify: `tests/test_cancelled_response_hook.py`
- Modify: `tests/test_multi_agent_bot.py`
- Modify: `tests/test_ai_user_id.py`

- [ ] **Step 1: Remove long backoff from cancel/error terminal updates**

Change terminal update retries so cancel/error/restart finalization does not sit behind the full `2+4+8+16+32` backoff while the lifecycle is blocked.
Add or replace tests for the actual broken paths:
- explicit cancel + terminal update returns `None`
- explicit cancel + terminal update raises a normal exception
- explicit cancel + terminal update raises `asyncio.CancelledError`

- [ ] **Step 2: Replace the canonical-final-content merge with one explicit rule**

Remove `_merge_final_completion_content()` and stop incrementally rewriting visible streamed text from `RunCompletedEvent.content`.
If provider canonical final content disagrees with already-visible streamed text, keep the visible streamed text unchanged during streaming and treat the canonical final content only as the clean-success final-body candidate for the one final replacement edit.
Plumb that candidate through the transport boundary explicitly by adding a separate field such as `canonical_final_body_candidate` to the streamed transport outcome and the finalization request path.
Cover providers that emit final body text only in `RunCompletedEvent.content`.

- [ ] **Step 3: Delete duplicate late-failure and cancellation helpers**

Remove:
- `_late_failure_without_visible`
- `_late_failure_with_preserved_stream`
- `_late_failure_with_visible_response`
- `_late_delivery_failure_outcome`
- `_late_stream_finalize_preserved_outcome`

Migrate the direct helper imports and tests in `tests/test_multi_agent_bot.py` at the same time instead of leaving them for a later red bar.

- [ ] **Step 4: Deduplicate only the remaining cancel-source failure-reason mapping in a boundary-safe way**

Do **not** change the merged-branch source of truth for cancellation provenance:
- `classify_cancel_source()` stays in runtime code
- `delivery_gateway.py` must not import `mindroom.orchestration.runtime`

If the remaining duplication is removed, do it by extracting only the `CancelSource -> failure_reason` mapping into a boundary-safe lower-level module or helper that both caller layers may import without a Tach violation.
Keep the current `user_stop` vs `sync_restart` vs generic `interrupted` behavior intact.

- [ ] **Step 5: Normalize retained cancellation reasons and route early empty-prompt exits through canonical terminal hook emission**

Fix both:
- `src/mindroom/response_runner.py`
- `src/mindroom/bot.py`

so empty prompts no longer return raw terminal outcomes that bypass `message:cancelled`.
Also stop collapsing retained `CancelledError` reasons to empty strings on the kept gateway paths.

- [ ] **Step 6: Run focused reliability, cancellation-provenance, and terminal-hook tests**

Run: `uv run pytest tests/test_streaming_finalize.py tests/test_cancelled_response_hook.py tests/test_multi_agent_bot.py tests/test_ai_user_id.py tests/test_sync_task_cancellation.py tests/test_ai_error_message_display.py tests/test_team_scheduler_context.py tests/test_streaming_behavior.py -q -n auto --no-cov`
Expected: PASS

- [ ] **Step 7: Commit the simplification cleanup**

```bash
git add src/mindroom/streaming.py src/mindroom/streaming_delivery.py src/mindroom/response_runner.py src/mindroom/delivery_gateway.py src/mindroom/bot.py tests/test_streaming_finalize.py tests/test_cancelled_response_hook.py tests/test_multi_agent_bot.py tests/test_ai_user_id.py
git commit -m "refactor: simplify terminal delivery cleanup paths"
```

### Task 5: Make Hook Contracts Plain, Consistent, and Testable

**Files:**
- Modify: `docs/hooks.md`
- Modify: `tests/test_cancelled_response_hook.py`

- [ ] **Step 1: Rewrite the hook docs in plain English and update the authoritative tables**

Document:
- `message:before_response` is pre-send only
- `message:final_response_transform` is text-only, one-shot, success-only, and best-effort
- `message:cancelled` means every terminal outcome other than clean success
- after visible streamed text lands, no hook may suppress/delete/redact that reply

Also update:
- the built-in events table
- the default-timeouts table
- the new `200ms` timeout for `message:final_response_transform`

- [ ] **Step 2: Add hook-contract regressions**

Add tests covering:
- empty-prompt paths still emit terminal hooks
- successful transformed streams emit `after_response`
- failed/cancelled streams emit `cancelled`
- retained cancellation paths preserve a non-empty `failure_reason`
- suppression is not available on `message:final_response_transform`

- [ ] **Step 3: Run the focused hook-contract suite**

Run: `uv run pytest tests/test_cancelled_response_hook.py -q -n auto --no-cov`
Expected: PASS

- [ ] **Step 4: Commit the contract cleanup**

```bash
git add docs/hooks.md tests/test_cancelled_response_hook.py
git commit -m "docs: clarify final response hook contract"
```

### Task 6: Full Verification and Diff Audit

**Files:**
- No intended production changes in this task

- [ ] **Step 1: Run the core targeted suites**

Run:

```bash
uv run pytest tests/test_hook_execution.py tests/test_streaming_behavior.py tests/test_streaming_finalize.py tests/test_multi_agent_bot.py tests/test_cancelled_response_hook.py tests/test_final_delivery.py tests/test_ai_user_id.py tests/test_sync_task_cancellation.py tests/test_ai_error_message_display.py tests/test_team_scheduler_context.py -q -n auto --no-cov
```

Expected: PASS

- [ ] **Step 2: Run pre-commit**

Run:

```bash
uv run pre-commit run --all-files
```

Expected: PASS

- [ ] **Step 3: Run the full backend suite**

Run:

```bash
just test-backend --no-cov
```

Expected: PASS

- [ ] **Step 4: Audit for dead simplified-away helpers**

Run:

```bash
rg -n "_late_failure_without_visible|_late_failure_with_preserved_stream|_late_failure_with_visible_response|_late_delivery_failure_outcome|_late_stream_finalize_preserved_outcome|_merge_final_completion_content" src/mindroom
```

Expected: no matches

- [ ] **Step 5: Audit for post-visible streamed mutation**

Run:

```bash
rg -n "apply_before_response|needs_final_edit|suppressed_by_hook|draft\\.suppress" src/mindroom/delivery_gateway.py
```

Expected: only pre-visible/non-streaming paths remain; no post-visible streamed mutation branch inside `finalize_streamed_response()`

- [ ] **Step 6: Commit the final verified state**

```bash
git status --short
git add <only the files changed in this plan>
git commit -m "refactor: simplify streamed terminal response semantics"
```
