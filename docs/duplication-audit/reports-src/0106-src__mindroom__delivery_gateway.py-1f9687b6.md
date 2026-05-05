## Summary

Top duplication candidates:

1. Matrix send/edit delivery repeats the same `get_latest_thread_event_id_if_needed` -> content builder -> `send_message_result`/`edit_message_result` -> `notify_outbound_message` flow in delivery gateway, hooks, scheduling, thread summaries, streaming, stale-stream cleanup, and Matrix conversation tools.
2. Terminal stream failure/cancellation outcome construction is repeated between `DeliveryGateway.finalize_streamed_response`, `response_runner.py`, `response_terminal.py`, and `streaming.py`, especially around `placeholder_only` vs `none` vs `visible_body`.
3. Terminal metadata edits repeat small `extra_content` updates that force `STREAM_STATUS_ERROR` in placeholder delivery failure and stale stream cleanup.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_is_placeholder_delivery_failure	function	lines 75-79	related-only	terminal_update_cancelled terminal_update_failed terminal_update_exception delivery_failed placeholder_only	src/mindroom/streaming.py:638,src/mindroom/streaming.py:660,src/mindroom/streaming.py:680
ResponseHookService	class	lines 83-188	none-found	ResponseHookService apply_before_response emit_after_response EVENT_MESSAGE_BEFORE_RESPONSE EVENT_MESSAGE_CANCELLED	none
ResponseHookService.apply_before_response	async_method	lines 88-111	none-found	apply_before_response ResponseDraft EVENT_MESSAGE_BEFORE_RESPONSE emit_transform	src/mindroom/hooks/execution.py:367,src/mindroom/hooks/context.py:235
ResponseHookService.apply_final_response_transform	async_method	lines 113-136	none-found	apply_final_response_transform FinalResponseDraft FinalResponseTransformContext emit_final_response_transform	src/mindroom/hooks/execution.py:385,src/mindroom/hooks/context.py:247
ResponseHookService.emit_after_response	async_method	lines 138-166	related-only	emit_after_response EVENT_MESSAGE_AFTER_RESPONSE ResponseResult response_event_id	src/mindroom/response_lifecycle.py:460
ResponseHookService.emit_cancelled_response	async_method	lines 168-188	none-found	emit_cancelled_response EVENT_MESSAGE_CANCELLED CancelledResponseInfo CancelledResponseContext	src/mindroom/hooks/context.py:450
SendTextRequest	class	lines 192-197	not-a-behavior-symbol	SendTextRequest dataclass fields	none
EditTextRequest	class	lines 201-206	not-a-behavior-symbol	EditTextRequest dataclass fields	none
FinalDeliveryRequest	class	lines 210-220	not-a-behavior-symbol	FinalDeliveryRequest dataclass fields	none
CancelledVisibleNoteRequest	class	lines 224-233	not-a-behavior-symbol	CancelledVisibleNoteRequest dataclass fields	none
_PlaceholderFailureUpdateRequest	class	lines 237-247	not-a-behavior-symbol	PlaceholderFailureUpdateRequest dataclass fields	none
MatrixCompactionLifecycle	class	lines 251-285	none-found	MatrixCompactionLifecycle CompactionLifecycleStart complete_success complete_failure	none
MatrixCompactionLifecycle.start	async_method	lines 258-264	none-found	send_compaction_lifecycle_start CompactionLifecycleStart lifecycle notice	none
MatrixCompactionLifecycle.progress	async_method	lines 266-271	none-found	edit_compaction_lifecycle_progress CompactionLifecycleProgress lifecycle notice	none
MatrixCompactionLifecycle.complete_success	async_method	lines 273-278	none-found	edit_compaction_lifecycle_success CompactionLifecycleSuccess lifecycle notice	none
MatrixCompactionLifecycle.complete_failure	async_method	lines 280-285	none-found	edit_compaction_lifecycle_failure CompactionLifecycleFailure lifecycle notice	none
StreamingDeliveryRequest	class	lines 289-303	not-a-behavior-symbol	StreamingDeliveryRequest dataclass fields	none
DeliveryGatewayDeps	class	lines 307-317	not-a-behavior-symbol	DeliveryGatewayDeps dataclass fields	none
FinalizeStreamedResponseRequest	class	lines 321-333	not-a-behavior-symbol	FinalizeStreamedResponseRequest dataclass fields	none
DeliveryGateway	class	lines 337-1521	duplicate-found	Matrix delivery gateway send edit stream finalize compaction placeholder	src/mindroom/hooks/sender.py:66,src/mindroom/scheduling.py:740,src/mindroom/thread_summary.py:396,src/mindroom/streaming.py:908,src/mindroom/custom_tools/matrix_conversation_operations.py:79,src/mindroom/matrix/stale_stream_cleanup.py:1120,src/mindroom/response_terminal.py:52,src/mindroom/response_runner.py:2270
DeliveryGateway._client	method	lines 342-348	none-found	Matrix client is not ready runtime.client	none
DeliveryGateway._current_stream_body	method	lines 350-352	related-only	rendered_body or empty current stream body	src/mindroom/streaming.py:511,src/mindroom/streaming.py:872
DeliveryGateway._visible_stream_event_id	method	lines 354-358	duplicate-found	visible_body_state visible_body last_physical_stream_event_id	src/mindroom/final_delivery.py:33
DeliveryGateway._interactive_response_for_visible_body	method	lines 361-385	related-only	parse_and_format_interactive canonical_body_candidate interactive_metadata formatted_text	src/mindroom/streaming.py:591,src/mindroom/custom_tools/matrix_conversation_operations.py:79,src/mindroom/custom_tools/matrix_conversation_operations.py:638
DeliveryGateway._cancelled_error_failure_reason	method	lines 388-395	related-only	CancelledError USER_STOP_CANCEL_MSG SYNC_RESTART_CANCEL_MSG cancel_failure_reason classify_cancel_source	src/mindroom/response_attempt.py:155,src/mindroom/response_runner.py:1207,src/mindroom/response_runner.py:2270
DeliveryGateway._cleanup_completed_placeholder_only_stream	async_method	lines 397-435	related-only	placeholder_only cleanup redact FinalDeliveryOutcome failure_reason	src/mindroom/response_terminal.py:33,src/mindroom/response_terminal.py:52
DeliveryGateway._redact_visible_response_event	async_method	lines 437-475	none-found	redact_message_event failed to redact suppressed response visible response cleanup	none
DeliveryGateway._finish_placeholder_delivery_failure	async_method	lines 477-521	duplicate-found	STREAM_STATUS_ERROR extra_content delivery failed placeholder terminal error	src/mindroom/matrix/stale_stream_cleanup.py:1190
DeliveryGateway.send_text	async_method	lines 523-574	duplicate-found	format_message_with_mentions get_latest_thread_event_id_if_needed send_message_result notify_outbound_message skip_mentions	src/mindroom/hooks/sender.py:76,src/mindroom/scheduling.py:740,src/mindroom/scheduling.py:883,src/mindroom/custom_tools/matrix_conversation_operations.py:79,src/mindroom/custom_tools/subagents.py:260
DeliveryGateway.edit_text	async_method	lines 576-639	duplicate-found	build_threaded_edit_content format_message_with_mentions edit_message_result notify_outbound_message src/mindroom/custom_tools/matrix_conversation_operations.py:638,src/mindroom/matrix/stale_stream_cleanup.py:1120,src/mindroom/streaming.py:948
DeliveryGateway.deliver_final	async_method	lines 641-840	related-only	FinalDeliveryOutcome deliver_final before_response suppress placeholder delivery_failed interactive parse	src/mindroom/response_runner.py:1829,src/mindroom/response_runner.py:2427
DeliveryGateway.deliver_cancelled_visible_note	async_method	lines 842-899	related-only	build_cancelled_response_update cancel_failure_reason cancellation note STREAM_STATUS src/mindroom/streaming.py:218,src/mindroom/streaming.py:496,src/mindroom/response_runner.py:1800
DeliveryGateway.send_compaction_lifecycle_start	async_method	lines 901-944	related-only	COMPACTION_NOTICE_CONTENT_KEY build_message_content m.notice send_message_result notify_outbound_message	src/mindroom/thread_summary.py:396,src/mindroom/history/types.py:145
DeliveryGateway.edit_compaction_lifecycle_progress	async_method	lines 946-960	none-found	edit_compaction_lifecycle_progress notice_event_id format_notice to_notice_metadata	none
DeliveryGateway.edit_compaction_lifecycle_success	async_method	lines 962-977	none-found	edit_compaction_lifecycle_success duration_ms format_notice to_notice_metadata	none
DeliveryGateway.edit_compaction_lifecycle_failure	async_method	lines 979-1004	related-only	Compaction failed continuing with trimmed history failure metadata	src/mindroom/history/runtime.py:575
DeliveryGateway._edit_compaction_lifecycle_notice	async_method	lines 1006-1049	duplicate-found	edit lifecycle notice build_message_content m.notice edit_message_result notify_outbound_message	src/mindroom/matrix/stale_stream_cleanup.py:1120,src/mindroom/custom_tools/matrix_conversation_operations.py:638
DeliveryGateway.deliver_stream	async_method	lines 1051-1091	related-only	send_streaming_response latest_thread_event_id conversation_cache preserve_existing_visible_on_empty_terminal	src/mindroom/streaming.py:1048
DeliveryGateway._finalize_visible_replacement_edit	async_method	lines 1093-1131	related-only	parse_and_format_interactive edit_text FinalDeliveryOutcome completed edited interactive_metadata	src/mindroom/delivery_gateway.py:759,src/mindroom/custom_tools/matrix_conversation_operations.py:638
DeliveryGateway._finalize_placeholder_only_stream_error	async_method	lines 1133-1174	related-only	placeholder_only stream error delivery_failed cleanup_completed_placeholder_only_stream	src/mindroom/response_terminal.py:52,src/mindroom/streaming.py:638
DeliveryGateway.finalize_streamed_response	async_method	lines 1176-1521	duplicate-found	finalize_streamed_response StreamTransportOutcome visible_body_state FinalDeliveryOutcome placeholder_only terminal_status	src/mindroom/streaming.py:511,src/mindroom/response_terminal.py:52,src/mindroom/response_runner.py:2270,src/mindroom/response_runner.py:2317,src/mindroom/response_runner.py:2353
```

## Findings

### 1. Matrix send/edit plus conversation-cache notification is repeated

`DeliveryGateway.send_text` and `_edit_compaction_lifecycle_notice` build Matrix content, send or edit it, then record `delivered.event_id` and `delivered.content_sent` in the conversation cache.
The same behavior appears in:

- `src/mindroom/hooks/sender.py:76` and `src/mindroom/hooks/sender.py:90`
- `src/mindroom/scheduling.py:740`, `src/mindroom/scheduling.py:883`, `src/mindroom/scheduling.py:1024`, and `src/mindroom/scheduling.py:1137`
- `src/mindroom/thread_summary.py:396` and `src/mindroom/thread_summary.py:424`
- `src/mindroom/custom_tools/matrix_conversation_operations.py:79`, `src/mindroom/custom_tools/matrix_conversation_operations.py:98`, `src/mindroom/custom_tools/matrix_conversation_operations.py:638`, and `src/mindroom/custom_tools/matrix_conversation_operations.py:657`
- `src/mindroom/custom_tools/subagents.py:260` and `src/mindroom/custom_tools/subagents.py:275`
- `src/mindroom/streaming.py:908`, `src/mindroom/streaming.py:919`, `src/mindroom/streaming.py:928`, and `src/mindroom/streaming.py:948`
- `src/mindroom/matrix/stale_stream_cleanup.py:1120` and `src/mindroom/matrix/stale_stream_cleanup.py:1135`

The repeated behavior is active and user-visible: successful Matrix delivery must update the thread cache so future thread relations point at the latest outbound event.
Differences to preserve include sender-domain choice, message vs notice content, edit vs send, room-mode handling, skip-mentions metadata, original-sender metadata, and error logging policy.

### 2. Stream terminal outcome handling repeats placeholder/visible-body state decisions

`DeliveryGateway.finalize_streamed_response` maps `StreamTransportOutcome` into `FinalDeliveryOutcome`.
Related and partially duplicated construction appears in:

- `src/mindroom/streaming.py:511`, which builds `StreamTransportOutcome` from the terminal streaming edit result.
- `src/mindroom/response_terminal.py:52`, which builds a canonical terminal `StreamTransportOutcome` from pending visible response state.
- `src/mindroom/response_runner.py:2270`, `src/mindroom/response_runner.py:2317`, and `src/mindroom/response_runner.py:2353`, which manually construct placeholder-only `StreamTransportOutcome` instances before calling the gateway.

This is duplication of domain rules rather than identical code.
The same fields are repeatedly coordinated: `last_physical_stream_event_id`, `terminal_status`, `rendered_body`, `visible_body_state`, and `failure_reason`.
Differences to preserve include whether a terminal failure already entered delivery, whether an existing visible event must be preserved, and whether a placeholder should be redacted or edited to a terminal failure note.

### 3. Terminal stream error metadata update is repeated

`DeliveryGateway._finish_placeholder_delivery_failure` creates `failure_extra_content` and forces `constants.STREAM_STATUS_KEY` to `constants.STREAM_STATUS_ERROR`.
`src/mindroom/matrix/stale_stream_cleanup.py:1190` performs the same metadata merge for stale stream cleanup.
Both preserve existing metadata and mark a visible stream as terminal error.
Differences to preserve include whether `None` should become `{STREAM_STATUS_KEY: STREAM_STATUS_ERROR}` and whether the caller includes tool trace and final delivery outcome data.

### 4. Visible body-state event-id selection duplicates an existing model property

`DeliveryGateway._visible_stream_event_id` returns the stream event id only when `visible_body_state == "visible_body"`.
`src/mindroom/final_delivery.py:33` already exposes `StreamTransportOutcome.has_visible_response` with the same state predicate.
The gateway still needs the event id, so this is small duplication, but it is a real repeated predicate.

## Proposed Generalization

1. Add a small Matrix delivery helper in `src/mindroom/matrix/client_delivery.py` that performs successful-delivery cache notification for both send and edit results.
2. Keep content construction at call sites, but use the helper for the repeated `send_message_result`/`edit_message_result` plus `conversation_cache.notify_outbound_message` pattern.
3. Add a tiny stream metadata helper near `streaming.py` or `matrix/stale_stream_cleanup.py`, such as `with_terminal_stream_error_status(content)`, to centralize the `STREAM_STATUS_ERROR` merge.
4. Consider replacing `_visible_stream_event_id` with a `StreamTransportOutcome.visible_event_id` property if the event-id selection appears in more files.
5. Do not generalize compaction lifecycle handling yet; it is mostly gateway-owned and only shares the generic Matrix notice send/edit shape.

## Risk/tests

Primary risks are thread relation regressions, missed conversation-cache updates, and preserving the wrong visible event during cancellation or terminal errors.
Any refactor should run focused tests for delivery gateway finalization, streaming terminal status handling, stale stream cleanup, hook message sending, scheduled workflow posting, Matrix conversation tools, and thread summary sends.
No production code was changed for this audit.
