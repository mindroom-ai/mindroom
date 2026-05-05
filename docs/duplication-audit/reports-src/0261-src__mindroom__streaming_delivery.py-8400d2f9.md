# Duplication Audit: `src/mindroom/streaming_delivery.py`

## Summary

Top duplication candidate: visible tool-call stream tracking in `_consume_streaming_chunks` overlaps with similar start/complete tracking in `src/mindroom/ai.py` and `src/mindroom/teams.py`.
The delivery queue ownership, request coalescing, worker-progress drain, and shutdown supervision code appears specific to streaming delivery, with only related task-wait/cancel idioms elsewhere.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_NonTerminalDeliveryError	class	lines 41-46	related-only	NonTerminalDeliveryError StreamingDeliveryError wrapper error delivery	src/mindroom/streaming.py:82; src/mindroom/streaming.py:134
_NonTerminalDeliveryError.__init__	method	lines 44-46	related-only	Exception wrapper __init__ self.error str(error)	src/mindroom/streaming.py:85
_StreamDeliveryShutdownTimeoutError	class	lines 49-50	related-only	ShutdownTimeoutError TimeoutError stream delivery shutdown timeout	src/mindroom/streaming.py:1140; src/mindroom/streaming.py:1178
_longest_common_prefix_len	function	lines 53-59	none-found	longest_common_prefix common prefix ToolTraceEntry prefix list comparison	none
_merge_tool_trace	function	lines 62-76	none-found	merge_tool_trace tool_trace shared prefix StructuredStreamChunk snapshot	src/mindroom/teams.py:2433; src/mindroom/streaming_delivery.py:219
_merge_prior_delta_at	function	lines 79-85	none-found	prior_delta_at min oldest unsent delta merge delivery request	src/mindroom/streaming.py:430; src/mindroom/streaming_delivery.py:424
_DeliveryRequest	class	lines 89-99	related-only	DeliveryRequest dataclass capture_completion progress_hint force_refresh boundary_refresh	src/mindroom/delivery_gateway.py:209; src/mindroom/delivery_gateway.py:288
_raise_progress_delivery_error	function	lines 102-104	not-a-behavior-symbol	raise stored error NoReturn helper	none
_queue_delivery_request	function	lines 107-148	none-found	queue delivery request put_nowait capture_completion emit_timing_event	progress	src/mindroom/streaming.py:1093; src/mindroom/tool_system/runtime_context.py:601
_flush_phase_boundary_if_needed	async_function	lines 151-168	none-found	phase boundary flush matching inflight capture wait_for_capture chars_since_last_update	src/mindroom/streaming.py:420; src/mindroom/streaming.py:139
_apply_visible_text_chunk	async_function	lines 171-202	related-only	visible text chunk apply_chunk replacement suffix prior_delta queue delivery	src/mindroom/streaming.py:468; src/mindroom/teams.py:2134
_consume_streaming_chunks	async_function	lines 205-329	duplicate-found	RunContentEvent ToolCallStartedEvent ToolCallCompletedEvent complete_pending_tool_block format_tool_started_event pending_tools	src/mindroom/ai.py:467; src/mindroom/ai.py:494; src/mindroom/ai.py:1240; src/mindroom/teams.py:2134; src/mindroom/teams.py:2161; src/mindroom/teams.py:2383
_drain_worker_progress_events	async_function	lines 332-362	none-found	worker progress events warmup_state needs_warmup_clear_edit progress_hint pump shutdown	src/mindroom/streaming.py:890; src/mindroom/tool_system/runtime_context.py:601
_shutdown_worker_progress_drain	async_function	lines 365-381	related-only	cancel task wait_for timeout shutdown set return exception	src/mindroom/orchestrator.py:1918; src/mindroom/background_tasks.py:81; src/mindroom/matrix/typing.py:86
_drive_stream_delivery	async_function	lines 384-506	none-found	delivery owner queue coalesce capture_completions boundary refresh phase_boundary_flush QueueEmpty	src/mindroom/streaming.py:139; src/mindroom/streaming.py:703; src/mindroom/streaming_delivery.py:384
_shutdown_stream_delivery	async_function	lines 509-534	related-only	put_nowait None wait timeout cancel task shutdown timeout exception	src/mindroom/background_tasks.py:102; src/mindroom/orchestrator.py:1884; src/mindroom/tools/shell.py:632
_cancel_stream_consumer	async_function	lines 537-545	related-only	cancel stream consumer wait_for suppress CancelledError TimeoutError	src/mindroom/orchestrator.py:1918; src/mindroom/background_tasks.py:81; src/mindroom/matrix/typing.py:86
_handle_auxiliary_task_completion	async_function	lines 548-574	related-only	FIRST_COMPLETED auxiliary task exception cancellation monitored_tasks discard	src/mindroom/orchestrator.py:1795; src/mindroom/orchestrator.py:1884
_consume_stream_with_progress_supervision	async_function	lines 577-618	related-only	asyncio wait FIRST_COMPLETED monitored_tasks stream_task progress_task delivery_task	src/mindroom/orchestrator.py:1795; src/mindroom/orchestrator.py:1884
```

## Findings

### 1. Visible tool-call stream tracking is repeated across stream producers and delivery

`src/mindroom/streaming_delivery.py:205` consumes `RunContentEvent`, `RunCompletedEvent`, `ToolCallStartedEvent`, and `ToolCallCompletedEvent`, keeps a pending tool stack, calls `format_tool_started_event`, updates visible text, and completes visible blocks with `complete_pending_tool_block`.
`src/mindroom/ai.py:467` and `src/mindroom/ai.py:494` perform the same start/complete bookkeeping for single-agent stream attempts, including pending tools, visible tool indices, formatted start text, completion block replacement, and warnings for missing starts.
`src/mindroom/teams.py:2134` and `src/mindroom/teams.py:2161` repeat the same pattern for team/member scoped tools, including pending lookup, formatted start text, completion block replacement, in-place `ToolTraceEntry` completion mutation, and missing-slot warnings.

The duplicated behavior is active and domain-level: all three sites translate Agno tool start/completion events into visible markdown plus structured `ToolTraceEntry` state.
Differences to preserve: `streaming_delivery.py` directly queues Matrix delivery and handles hidden tool calls with warmup/progress updates; `ai.py` tracks retry/attempt state and completed tools; `teams.py` scopes pending tools by member/team and emits `StructuredStreamChunk` snapshots.

### 2. Task cancellation and shutdown patterns are related, but not worth generalizing from this module alone

`src/mindroom/streaming_delivery.py:365`, `src/mindroom/streaming_delivery.py:509`, and `src/mindroom/streaming_delivery.py:537` cancel or drain tasks with bounded waits and exception normalization.
Related patterns exist in `src/mindroom/orchestrator.py:1918`, `src/mindroom/background_tasks.py:81`, and several other runtime modules.
The semantics differ enough that a shared helper would need parameters for sentinel queue shutdown, cancellation timeout, exception return vs suppress, and timeout error construction.

### 3. Delivery request coalescing is local to streaming delivery

`src/mindroom/streaming_delivery.py:384` drains queued `_DeliveryRequest` values, merges flags, keeps the oldest unsent `prior_delta_at`, handles capture futures, and makes the final choice among phase-boundary flush, force refresh, boundary refresh, and throttled send.
No equivalent queue owner or request coalescer was found elsewhere under `src`.
The nearby dataclasses in `src/mindroom/delivery_gateway.py:209` and `src/mindroom/delivery_gateway.py:288` are public request envelopes for Matrix delivery, not behavioral duplicates of `_DeliveryRequest`.

## Proposed generalization

Consider a focused helper for tool stream lifecycle bookkeeping in `src/mindroom/tool_system/events.py` or a new nearby module such as `src/mindroom/tool_system/stream_trace.py`.
It could expose small pure helpers for starting a visible tool entry, finding/completing a pending entry, and mutating the corresponding `ToolTraceEntry` slot.
Keep Matrix delivery queueing, team/member scoping, retry state, and structured chunk emission at the current call sites.

No refactor is recommended for delivery queue coalescing, worker-progress drain, or shutdown supervision based on this audit.

## Risk/tests

Risks for a tool-tracking refactor are ordering regressions, incorrect matching when tool call IDs are absent, visible marker replacement changes, and broken structured tool-trace metadata.
Tests should cover single-agent streaming with shown and hidden tool calls, team/member tool calls, missing completion starts, completed trace mutation, and mixed `StructuredStreamChunk` trace snapshots.
For delivery-specific code, existing tests should continue to focus on throttling, phase-boundary capture futures, boundary refresh timing, non-terminal delivery failures, and worker progress cleanup.
