## Summary

Top duplication candidate: `build_terminal_stream_transport_outcome()` centralizes a terminal placeholder-only `StreamTransportOutcome`, but `src/mindroom/response_runner.py` still hand-builds the same outcome in three AI early-failure branches.
The event-id selection in `PendingVisibleResponse.terminal_event_id` and placeholder classification in `PendingVisibleResponse.is_placeholder_only` are also repeated inline in those branches.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
PendingVisibleResponse	class	lines 14-43	duplicate-found	"tracked_event_id run_message_id existing_event_id existing_event_is_placeholder", "existing_event_id if request.existing_event_is_placeholder", "visible_body_state=\"placeholder_only\""	src/mindroom/response_runner.py:1328-1345, src/mindroom/response_runner.py:2273-2286, src/mindroom/response_runner.py:2310-2323, src/mindroom/response_runner.py:2346-2359
PendingVisibleResponse.terminal_event_id	method	lines 23-31	duplicate-found	"tracked_event_id or", "run_message_id", "existing_event_id if request.existing_event_is_placeholder"	src/mindroom/response_runner.py:1328-1339, src/mindroom/response_runner.py:2273-2275, src/mindroom/response_runner.py:2310-2312, src/mindroom/response_runner.py:2346-2348
PendingVisibleResponse.is_placeholder_only	method	lines 33-43	duplicate-found	"rendered_body=PROGRESS_PLACEHOLDER", "visible_body_state=\"placeholder_only\"", "placeholder_progress_sent"	src/mindroom/response_runner.py:2280-2286, src/mindroom/response_runner.py:2317-2323, src/mindroom/response_runner.py:2353-2359, src/mindroom/streaming.py:870-881
build_terminal_stream_transport_outcome	function	lines 46-64	duplicate-found	"StreamTransportOutcome(", "terminal_status=\"cancelled\"", "terminal_status=\"error\"", "visible_body_state=\"placeholder_only\""	src/mindroom/response_runner.py:1332-1345, src/mindroom/response_runner.py:2023-2036, src/mindroom/response_runner.py:2277-2286, src/mindroom/response_runner.py:2314-2323, src/mindroom/response_runner.py:2350-2359, src/mindroom/streaming.py:102-130, src/mindroom/streaming.py:547-566, src/mindroom/streaming.py:575-601, src/mindroom/streaming.py:632-682
```

## Findings

1. `response_runner.py` repeats the terminal placeholder-only stream outcome that `response_terminal.py` exists to build.

   In `src/mindroom/response_runner.py:2273-2286`, `src/mindroom/response_runner.py:2310-2323`, and `src/mindroom/response_runner.py:2346-2359`, the AI early-delivery failure path selects `tracked_event_id` first, then an existing placeholder event, and constructs `StreamTransportOutcome(last_physical_stream_event_id=event_id, rendered_body=PROGRESS_PLACEHOLDER, visible_body_state="placeholder_only", failure_reason=...)`.
   That is functionally the same terminal cleanup contract as `PendingVisibleResponse.terminal_event_id`, `PendingVisibleResponse.is_placeholder_only()`, and `build_terminal_stream_transport_outcome()` in `src/mindroom/response_terminal.py:23-64`.
   The important difference is that the AI early-failure branches intentionally do not use `run_message_id` directly in the expression; they first fold it into `tracked_event_id` with `tracked_event_id = tracked_event_id or run_message_id`.

2. `streaming.py` has related outcome-building behavior, but it is not a direct duplicate of this module.

   `src/mindroom/streaming.py:102-130` and `src/mindroom/streaming.py:547-682` also construct `StreamTransportOutcome` values and classify visible body state.
   Those paths preserve committed streamed body, canonical final-body candidates, and interactive metadata, so they are broader than `response_terminal.py`.
   `src/mindroom/streaming.py:870-881` has a related placeholder snapshot rule, but it operates on `StreamingResponse` committed-state fields rather than pending external event candidates.

3. `delivery_gateway.py` consumes the same terminal facts but does not duplicate construction.

   `src/mindroom/delivery_gateway.py:1181-1325` branches on `StreamTransportOutcome.terminal_status` and `visible_body_state`, and `src/mindroom/delivery_gateway.py:1133-1174` handles placeholder-only cleanup.
   This is downstream interpretation of the outcome, not duplicated building logic.

## Proposed generalization

Use `PendingVisibleResponse` and `build_terminal_stream_transport_outcome()` in the three AI early-failure branches in `src/mindroom/response_runner.py:2268-2359`.
Keep the existing behavior by passing `tracked_event_id=tracked_event_id or run_message_id`, `run_message_id=None`, `existing_event_id=request.existing_event_id`, and `existing_event_is_placeholder=request.existing_event_is_placeholder`.

Minimal refactor plan:

1. In each early-failure branch, compute `tracked_event_id = tracked_event_id or run_message_id` as today.
2. Replace the repeated `event_id = ...` and inline `StreamTransportOutcome(...)` blocks with `build_terminal_stream_transport_outcome(PendingVisibleResponse(...), ...)`.
3. Preserve the existing `FinalDeliveryOutcome(event_id=None, ...)` fallback when the helper returns no terminal event.
4. Add or adjust focused tests for tracked event, existing placeholder, and no-event early-failure outcomes.

## Risk/tests

Risk is low if the AI path keeps folding `run_message_id` into `tracked_event_id` before constructing `PendingVisibleResponse`.
The main regression risk is changing the no-event fallback or accidentally treating a non-placeholder existing event as placeholder-only.
Tests should cover cancelled early failure, error early failure, and the later `delivery_cancelled` fallback around `src/mindroom/response_runner.py:2268-2359`.
