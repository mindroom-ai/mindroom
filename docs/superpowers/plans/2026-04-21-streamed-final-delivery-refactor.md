# Streamed Final Delivery Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current streamed-finalization patch stack with one canonical terminal-delivery model that owns final visibility, terminal status, suppression, cancellation, hook emission, and post-response effects.

**Architecture:** Start from `origin/main` and refactor the streamed and non-streamed response paths so they both cross the shared downstream boundary as the same immutable `FinalDeliveryOutcome` state machine. Keep `streaming.py` responsible only for incremental rendering plus terminal transport facts, make `delivery_gateway.py` the sole owner of terminal delivery decisions and terminal hook emission, explicitly migrate direct `send_text()` producers such as `send_skill_command_response_locked()` into that same canonical terminal boundary, and make `response_lifecycle.py`, `response_runner.py`, and `post_response_effects.py` consume only the canonical outcome instead of reconstructing meaning from `DeliveryResult`, `tracked_event_id`, `resolved_event_id`, or mutable extra content.

**Tech Stack:** Python 3.12, asyncio, Matrix nio client helpers, dataclasses, immutable snapshots, pytest, existing MindRoom hook and delivery abstractions.

---

## Scope

This is a bounded subsystem refactor, not a full rewrite.

The implementation branch must start from `origin/main`, not from the current PR branch.

Use the current PR branch only as a source of regression scenarios and failure matrices.

Non-goals are general hook-framework redesign, Matrix transport redesign, or unrelated cleanup outside response finalization.

## Target Invariants

1. There is exactly one canonical terminal-delivery object, and it is a state machine, not a bag of independent booleans.

2. The canonical object must represent both:
   - the final user-visible outcome, and
   - the last physically visible streamed event when that differs from the final outcome.

3. `delivery_gateway.py` is the sole owner of terminal delivery semantics for both streaming and non-streaming paths.

4. Only the terminal delivery coordinator may emit `after_response` or `cancelled_response`.

5. `after_response` fires exactly once and only after successful final visible delivery.

6. `cancelled_response` is mutually exclusive with `after_response`.

7. If a visible streamed event already exists, later hook failure, terminal transport failure, or cancellation cannot erase that physical visibility fact logically.

8. Suppression is terminal state, and suppression cleanup/redaction failure is an explicit canonical outcome, not an implied success.

9. Post-response effects consume only the canonical outcome. They must not infer visibility from `resolved_event_id`, `tracked_event_id`, provisional `DeliveryResult`, placeholder adoption, or mutable metadata.

10. Terminal sends are never retried when `event_id is None`. No terminal path may create a second standalone visible reply.

11. Cancellation and restart finalization never sleep behind retry backoff.

12. “Visible output” is defined at the final rendered body layer, not from chunk counters, reasoning counters, or tool counters.

13. Placeholder-only visibility never counts as successful final visible delivery.

14. No path may finish as visible `Thinking...` when there is no rendered visible output.

15. Failure or cancellation inside `after_response` or `cancelled_response` must never mutate the canonical outcome, trigger fallback re-emission, or schedule a second emission attempt.

16. Terminal status, AI-run metadata, and terminal stream metadata are coordinator-owned facts derived from `StreamTransportOutcome` and frozen before hooks run.

17. Canonical event IDs are logical visible response-event IDs, never raw edit-event IDs.

18. Any surviving `tracked_event_id`, `DeliveryResult`, or similar helpers are transport-, telemetry-, or recorder-only and never semantic sources of truth.

19. Every terminal case has exactly one legal constructor/state mapping. No terminal case may be representable in two different canonical shapes.

20. If suppression wins after a visible stream event already exists, cleanup/redaction is mandatory. The only legal terminal outcomes are cleanup success or explicit cleanup failure.

## File Map

**Create:**

- `src/mindroom/final_delivery.py`
  Small pure module that defines immutable terminal state types and validation helpers.

- `tests/test_final_delivery.py`
  Canonical invariant tests for terminal delivery, suppression, hook emission, and post-response effects.

**Modify:**

- `src/mindroom/streaming.py`
  Narrow responsibility to incremental rendering and terminal transport facts only.

- `src/mindroom/delivery_gateway.py`
  Become the only owner of terminal delivery coordination and terminal hook emission.

- `src/mindroom/response_lifecycle.py`
  Stop deciding delivery semantics and become a thin consumer of canonical outcomes.

- `src/mindroom/response_runner.py`
  Convert both streaming and non-streaming terminal flows, including direct `send_text()` producers, into the same canonical outcome before shared downstream processing.

- `src/mindroom/post_response_effects.py`
  Consume only canonical terminal outcomes.

- `src/mindroom/ai.py`
  Keep recorder-facing assistant text aligned with final-event-only visible content.

- `tests/test_streaming_finalize.py`
  Keep transport-focused streaming tests only.

- `tests/test_ai_user_id.py`
  Keep AI and recorder regressions only.

## Canonical Types

The key design change is to cross the layer boundaries with typed facts instead of tuples, event IDs, and mutable dicts.

### `StreamTransportOutcome`

This is a transport-facts object returned by `streaming.py`.

It must be sufficient for the coordinator to reason about transport without inspecting side fields.

```python
@dataclass(frozen=True)
class StreamTransportOutcome:
    last_physical_stream_event_id: str | None
    terminal_operation: Literal["none", "send", "edit"]
    terminal_result: Literal["not_attempted", "succeeded", "failed", "cancelled"]
    terminal_status: Literal["completed", "cancelled", "error"]
    rendered_body: str | None
    visible_body_state: Literal["none", "placeholder_only", "visible_body"]
    failure_reason: str | None = None
```

Notes:

- `last_physical_stream_event_id` is the last logical response event that was actually visible in Matrix, even if later terminal delivery fails.
- `visible_body_state` distinguishes real visible content from placeholder-only visibility.
- `rendered_body` means the exact Matrix body that would be sent after interactive formatting, tool filtering, and placeholder normalization. It is not raw accumulated text.
- This object replaces `(event_id, accumulated_text)` as the streaming terminal boundary.

### `FinalDeliveryOutcome`

This is the only semantic terminal-delivery object used downstream.

It must not carry overlapping booleans like `delivered`, `suppressed`, or `cancelled`.

```python
@dataclass(frozen=True)
class FinalDeliveryOutcome:
    state: Literal[
        "final_visible_delivery",
        "kept_prior_visible_stream_after_completed_terminal_failure",
        "kept_prior_visible_stream_after_cancel",
        "kept_prior_visible_stream_after_error",
        "cancelled_with_visible_note",
        "cancelled_without_visible_response",
        "suppressed_without_visible_response",
        "suppressed_redacted",
        "suppression_cleanup_failed",
        "error_with_visible_response",
        "error_without_visible_response",
    ]
    terminal_status: Literal["completed", "cancelled", "error"]
    final_visible_event_id: str | None
    last_physical_stream_event_id: str | None
    final_visible_body: str | None
    failure_reason: str | None = None
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    extra_content: Mapping[str, Any] = EMPTY_MAPPING
    option_map: Mapping[str, str] | None = None
    options_list: tuple[Mapping[str, str], ...] | None = None
```

Rules:

- Use validating constructors or pure factory helpers so illegal combinations cannot be instantiated.
- Use frozen snapshots only. `tool_trace`, `extra_content`, and `options_list` must not be mutable containers.
- `final_visible_event_id` is the final logical user-visible response-event ID only, never a raw edit-event ID.
- `last_physical_stream_event_id` preserves the “something already became visible” fact even when the final state is cancellation, suppression cleanup failure, or kept-prior-stream fallback.
- Hook selection and hook emission stay coordinator-private. They do not cross the shared downstream boundary as pending work.
- If suppression wins and `last_physical_stream_event_id is None`, the only legal suppression state is `suppressed_without_visible_response`.
- If suppression wins and `last_physical_stream_event_id is not None`, the only legal suppression states are `suppressed_redacted` or `suppression_cleanup_failed`.

No downstream layer may infer delivery meaning from `resolved_event_id`, `tracked_event_id`, placeholder adoption, or provisional `DeliveryResult`.

### Legal Constructor Mapping

The canonical model must include factory helpers or a legal-state table for every terminal case.

Minimum required mappings:

- successful final visible send/edit -> `final_visible_delivery`
- cancellation with a final visible cancel note -> `cancelled_with_visible_note`
- cancellation before any visible response exists -> `cancelled_without_visible_response`
- cancellation after a prior visible stream exists but no successful final visible delivery lands -> `kept_prior_visible_stream_after_cancel`
- error after a prior visible stream exists but no successful final visible delivery lands -> `kept_prior_visible_stream_after_error`
- completed terminal re-edit failure after a prior visible stream exists -> `kept_prior_visible_stream_after_completed_terminal_failure`
- suppression before any visible response exists -> `suppressed_without_visible_response`
- suppression after a visible response exists and cleanup succeeds -> `suppressed_redacted`
- suppression after a visible response exists and cleanup fails or is cancelled -> `suppression_cleanup_failed`
- error with a successful final visible error body -> `error_with_visible_response`
- error before any visible response exists -> `error_without_visible_response`

## Task 1: Introduce Canonical Terminal Types

**Files:**

- Create: `src/mindroom/final_delivery.py`
- Test: `tests/test_final_delivery.py`

- [ ] **Step 1: Write failing contract tests for the typed terminal models.**

```python
def test_kept_existing_visible_stream_keeps_physical_visibility_separate_from_final_success() -> None:
    outcome = FinalDeliveryOutcome.keep_prior_visible_stream_after_cancel(
        last_physical_stream_event_id="$stream",
        failure_reason="terminal_cancelled_after_visible_stream",
    )
    assert outcome.state == "kept_prior_visible_stream_after_cancel"
    assert outcome.final_visible_event_id is None
    assert outcome.last_physical_stream_event_id == "$stream"
```

- [ ] **Step 2: Run the contract tests on `origin/main` and confirm failure.**

Run: `uv run pytest tests/test_final_delivery.py -q -k "contract or canonical"`

Expected: FAIL because the new types and constructors do not exist yet.

- [ ] **Step 3: Create `src/mindroom/final_delivery.py` with immutable `StreamTransportOutcome` and `FinalDeliveryOutcome` plus validating constructors and an explicit legal-state table.**

- [ ] **Step 4: Keep this module pure.**

Do not move gateway policy or Matrix transport calls into it.

- [ ] **Step 5: Run the contract tests until they pass.**

Run: `uv run pytest tests/test_final_delivery.py -q -k "contract or canonical"`

Expected: PASS.

- [ ] **Step 6: Commit the type introduction.**

```bash
git add src/mindroom/final_delivery.py tests/test_final_delivery.py
git commit -m "refactor: add canonical terminal delivery types"
```

## Task 2: Make Streaming Return Typed Transport Facts

**Files:**

- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/ai.py`
- Modify: `tests/test_streaming_finalize.py`
- Modify: `tests/test_ai_user_id.py`
- Modify: `tests/test_final_delivery.py`

- [ ] **Step 1: Write failing transport tests for the real boundary cases.**

Add explicit tests for:

- terminal send is never retried when `event_id is None`
- cancellation and restart finalization never sleep behind retry backoff
- placeholder-only visibility is represented as `placeholder_only`, not visible success
- reasoning-only, hidden-tool-only, whitespace-only, and final-event-only-`None` cases do not count as visible output
- rendered body is defined from the exact final Matrix `display_text`, after placeholder normalization
- final-event-only content updates recorder-facing assistant text
- team `ReplacementStreamingResponse` path obeys the same transport rules

- [ ] **Step 2: Run the new transport tests and confirm failure on `origin/main`.**

Run: `uv run pytest tests/test_streaming_finalize.py tests/test_ai_user_id.py -q -k "retry or cancel or placeholder or hidden_tool or final_event"`

Expected: FAIL because the old boundary still returns partial facts and terminal retries still live inline.

- [ ] **Step 3: Replace `(event_id, accumulated_text)` and ad hoc stream-finalization state with `StreamTransportOutcome`.**

- [ ] **Step 4: Remove terminal retry-on-send completely.**

Rule: if `event_id is None`, terminal send gets one attempt, full stop.

- [ ] **Step 5: Make cancel and restart finalization bypass retry sleep entirely.**

Rule: no backoff on cancel or restart paths.

- [ ] **Step 6: Compute visible output from the rendered final body layer.**

Do not rely on reasoning counters, tool counters, or raw chunk non-emptiness.

Treat rendered body as the exact final Matrix `display_text` after formatting and placeholder normalization.

- [ ] **Step 7: Keep recorder-facing assistant text aligned with final-event-only visible content.**

- [ ] **Step 8: Run the focused transport and recorder tests.**

Run: `uv run pytest tests/test_streaming_finalize.py tests/test_ai_user_id.py tests/test_final_delivery.py -q -k "retry or cancel or placeholder or hidden_tool or final_event"`

Expected: PASS.

- [ ] **Step 9: Commit the transport boundary refactor.**

```bash
git add src/mindroom/streaming.py src/mindroom/ai.py tests/test_streaming_finalize.py tests/test_ai_user_id.py tests/test_final_delivery.py
git commit -m "refactor: return typed streaming transport facts"
```

## Task 3: Make Delivery Gateway the Sole Terminal Coordinator

**Files:**

- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/final_delivery.py`
- Modify: `tests/test_final_delivery.py`

- [ ] **Step 1: Write failing coordinator tests for the semantic failure matrix.**

Add explicit tests for:

- failed hook re-edit after a visible stream already exists yields `kept_prior_visible_stream_after_completed_terminal_failure`
- terminal transport failure after a visible stream already exists also yields the corresponding kept-prior-visible-stream terminal state
- plain suppression before any visible response exists yields `suppressed_without_visible_response`
- suppression after a visible stream exists and cleanup succeeds yields `suppressed_redacted`
- suppression after a visible stream exists and cleanup fails yields `suppression_cleanup_failed`
- `after_response` and `cancelled_response` are emitted only here
- `after_response` failure or cancellation does not demote a successful visible delivery and does not trigger a second emission later
- `cancelled_response` failure or cancellation does not mutate the canonical outcome and does not trigger fallback re-emission
- terminal metadata snapshots are frozen before hooks run, and hooks cannot override terminal status or coordinator-owned metadata

- [ ] **Step 2: Run the coordinator tests and confirm failure.**

Run: `uv run pytest tests/test_final_delivery.py -q -k "coordinator or suppression or after_response or cancelled_response"`

Expected: FAIL because `origin/main` still splits terminal ownership across gateway, lifecycle, and runner.

- [ ] **Step 3: Introduce one terminal-delivery coordinator in `delivery_gateway.py`.**

This coordinator must own:

- hook application
- final streamed re-edit decisions
- fallback to already-visible stream events
- suppression cleanup and cleanup-failure terminalization
- `after_response`
- `cancelled_response`
- freezing terminal status and metadata snapshots before hook application

- [ ] **Step 4: Apply the same coordinator ownership to non-streaming terminal delivery.**

Do not leave non-streaming final delivery with separate hook-emission ownership.

Direct `send_text()` producers such as `send_skill_command_response_locked()` must either:

- migrate into the same terminal coordinator, or
- be called out explicitly as out of scope for this refactor.

Prefer migration.

- [ ] **Step 4a: Move cancellation/restart visible-note delivery behind the gateway coordinator as well.**

Do not leave cancellation/restart terminal note edits in `response_runner.py`.

- [ ] **Step 5: Keep “transport action” separate from “terminal outcome.”**

Do not expose a public cross-layer `TerminalDeliveryDecision` enum unless it remains tiny and private.

- [ ] **Step 6: Run the coordinator tests.**

Run: `uv run pytest tests/test_final_delivery.py -q -k "coordinator or suppression or after_response or cancelled_response"`

Expected: PASS.

- [ ] **Step 7: Commit the coordinator refactor.**

```bash
git add src/mindroom/delivery_gateway.py src/mindroom/final_delivery.py tests/test_final_delivery.py
git commit -m "refactor: centralize terminal delivery in gateway"
```

## Task 4: Delete Lifecycle-Owned Delivery Semantics

**Files:**

- Modify: `src/mindroom/response_lifecycle.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `tests/test_final_delivery.py`

- [ ] **Step 1: Write failing tests that prove lifecycle no longer reconstructs delivery meaning.**

Add explicit tests for:

- streamed path no longer uses `resolve_response_event_id()` as semantic fallback
- lifecycle no longer decides cancellation-vs-visible-delivery meaning
- lifecycle no longer owns any repair semantics
- any surviving `tracked_event_id` or `DeliveryResult` use is recorder/transport-only and never semantic

- [ ] **Step 2: Run the lifecycle tests and confirm failure.**

Run: `uv run pytest tests/test_final_delivery.py -q -k "lifecycle or resolve_response_event_id or cancel_semantics"`

Expected: FAIL because `origin/main` still reconstructs meaning from `DeliveryResult`, `tracked_event_id`, and `resolved_event_id`.

- [ ] **Step 3: Change `ResponseLifecycle.finalize()` to consume and return canonical `FinalDeliveryOutcome`, not `str | None` as the meaningful semantic result.**

- [ ] **Step 4: Remove streamed terminal semantics from `resolve_response_event_id()` and `_is_cancelled_delivery_result()`.**

If those helpers survive, they must not be part of streamed terminal semantics.

- [ ] **Step 5: Update team and agent paths in `response_runner.py` to pass canonical terminal outcomes through unchanged.**

- [ ] **Step 6: Run the lifecycle and runner tests.**

Run: `uv run pytest tests/test_final_delivery.py tests/test_streaming_finalize.py -q -k "lifecycle or resolve_response_event_id or team_path"`

Expected: PASS.

- [ ] **Step 7: Commit the lifecycle simplification.**

```bash
git add src/mindroom/response_lifecycle.py src/mindroom/response_runner.py tests/test_final_delivery.py tests/test_streaming_finalize.py
git commit -m "refactor: remove lifecycle-owned delivery semantics"
```

## Task 5: Make Shared Downstream Effects Use Canonical Outcome Only

**Files:**

- Modify: `src/mindroom/post_response_effects.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `tests/test_final_delivery.py`

- [ ] **Step 1: Write failing tests for shared downstream behavior using canonical outcomes only.**

Add explicit tests for:

- run linkage persistence
- interactive registration
- compaction notice dispatch
- thread summary enqueue
- cancelled outcomes that still have a physically visible prior stream event but no successful final visible delivery
- suppression cleanup failure outcomes
- direct non-streamed `send_text()` producer path constructs the same canonical `FinalDeliveryOutcome`

- [ ] **Step 2: Run the downstream tests and confirm failure.**

Run: `uv run pytest tests/test_final_delivery.py -q -k "post_effects or thread_summary or interactive or persistence"`

Expected: FAIL because `origin/main` still keys behavior off `resolved_event_id`, `DeliveryResult`, and cancellation helpers.

- [ ] **Step 3: Change the shared downstream boundary so both streamed and non-streamed paths pass the same canonical `FinalDeliveryOutcome`.**

This explicitly includes direct-send producers like `send_skill_command_response_locked()`.

- [ ] **Step 4: Remove `resolved_event_id` and independent delivery facts from the post-effects interface.**

If `ResponseOutcome` survives, it must carry the canonical final outcome and no parallel delivery source of truth.

- [ ] **Step 5: Gate all post-response effects directly on canonical state and canonical final visible event identity.**

- [ ] **Step 6: Run the downstream tests.**

Run: `uv run pytest tests/test_final_delivery.py -q -k "post_effects or thread_summary or interactive or persistence"`

Expected: PASS.

- [ ] **Step 7: Commit the post-effects rewrite.**

```bash
git add src/mindroom/post_response_effects.py src/mindroom/response_runner.py tests/test_final_delivery.py
git commit -m "refactor: drive shared downstream effects from canonical terminal outcome"
```

## Task 6: Port Invariant Regressions and Delete Transitional State Consumers

**Files:**

- Modify: `tests/test_streaming_finalize.py`
- Modify: `tests/test_ai_user_id.py`
- Modify: `tests/test_final_delivery.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/response_lifecycle.py`
- Modify: `src/mindroom/post_response_effects.py`

- [ ] **Step 1: Port only the true invariants from the current PR branch.**

Do not port tests that only encode patch-stack mechanics.

- [ ] **Step 2: Remove remaining alternate semantic inputs from shared downstream code.**

Examples to delete or neutralize:

- streamed-path dependence on `resolve_response_event_id()`
- streamed-path semantic use of `_is_cancelled_delivery_result()`
- post-effects gating on independent `resolved_event_id`
- lifecycle reconstruction from placeholder-derived state

- [ ] **Step 3: Run the focused regression suites.**

Run: `uv run pytest tests/test_final_delivery.py tests/test_streaming_finalize.py tests/test_ai_user_id.py -q`

Expected: PASS.

- [ ] **Step 4: Run the broader related suites.**

Run: `uv run pytest tests/test_presence_based_streaming.py tests/test_thread_mode.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the invariant cleanup.**

```bash
git add src/mindroom/response_runner.py src/mindroom/response_lifecycle.py src/mindroom/post_response_effects.py tests/test_final_delivery.py tests/test_streaming_finalize.py tests/test_ai_user_id.py tests/test_presence_based_streaming.py tests/test_thread_mode.py
git commit -m "test: keep only canonical terminal delivery invariants"
```

## Review Checklist

- Is `FinalDeliveryOutcome` a real state machine instead of overlapping flags.

- Is `last_physical_stream_event_id` distinct from `final_visible_event_id`.

- Does the legal-state table make cancelled/error-after-prior-visible-stream cases unique instead of ambiguous.

- Does `streaming.py` return a typed `StreamTransportOutcome` instead of partial tuples and side fields.

- Does `delivery_gateway.py` exclusively own `after_response` and `cancelled_response`.

- Is hook emission private to the coordinator rather than exposed as a downstream action field.

- Can a visible streamed event still be lost logically after hook failure, transport failure, or cancellation.

- Is there an explicit `suppressed_without_visible_response` state.

- Is suppression cleanup failure an explicit canonical outcome.

- Is cleanup mandatory whenever suppression wins after a visible stream event exists.

- Is terminal retry-on-send fully removed when `event_id is None`.

- Can cancel or restart still stall behind retry backoff.

- Is visible output decided from the rendered final body layer.

- Can placeholder-only visibility still count as final visible success.

- Are canonical event IDs defined as logical response-event IDs rather than raw edit-event IDs.

- Do post-response effects still key off `resolved_event_id`, provisional `DeliveryResult`, or other parallel state.

- Do both streaming and non-streaming terminal paths cross the same canonical downstream boundary.

## Final Verification

- [ ] Run: `uv run pytest tests/test_final_delivery.py tests/test_streaming_finalize.py tests/test_ai_user_id.py tests/test_presence_based_streaming.py tests/test_thread_mode.py -q`

- [ ] Run: `uv run pre-commit run --all-files`

- [ ] Run: `rg -n "emit_after_response|emit_cancelled_response" src/mindroom`

Expected: terminal hook emission should exist only in the terminal delivery coordinator.

- [ ] Run: `rg -n "resolve_response_event_id|_is_cancelled_delivery_result" src/mindroom/response_lifecycle.py src/mindroom/post_response_effects.py src/mindroom/response_runner.py`

Expected: no streamed-path semantic dependence remains after the coordinator is introduced.

- [ ] Review final diff against `origin/main` with `git diff --stat origin/main..HEAD`

- [ ] Verify no unrelated local artifacts such as `config.yaml` or scratch directories are included.

## Execution Notes

Do not cherry-pick production code from the current PR branch.

Do manually port invariant tests and the smallest useful helper code from the current PR branch.

Prefer deleting partial-state consumers over adding another coordination flag.

If a proposed fix requires another mutable repair field, another fallback event-ID inference, or another lifecycle-owned terminal special case, stop and redesign that step before continuing.
