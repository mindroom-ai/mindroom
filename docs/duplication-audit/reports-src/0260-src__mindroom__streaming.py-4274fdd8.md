## Summary

Top duplication candidates for `src/mindroom/streaming.py`:

1. Cancellation provenance logging in `_log_stream_cancellation` duplicates the generic `log_cancelled_response` helper in `src/mindroom/response_attempt.py`.
2. Streaming send/edit delivery repeats the same `send_message_result` / `edit_message_result` plus `conversation_cache.notify_outbound_message` shape used by `DeliveryGateway.send_text`, `DeliveryGateway.edit_text`, stale stream cleanup, and several smaller senders.
3. Streaming warmup visible-body preservation is related to stale stream cleanup's visible-body preservation, but the flows differ enough that a shared helper is only worth considering if more call sites appear.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
StreamingDeliveryError	class	lines 82-99	related-only	StreamingDeliveryError StreamTransportOutcome delivery error accumulated_text tool_trace	src/mindroom/final_delivery.py:18; src/mindroom/response_terminal.py:42; src/mindroom/delivery_gateway.py:1053
StreamingDeliveryError.__init__	method	lines 85-99	related-only	Exception wrapper transport_outcome accumulated_text tool_trace	src/mindroom/final_delivery.py:18; src/mindroom/response_runner.py:1296
_build_streaming_delivery_error	function	lines 102-131	related-only	build StreamTransportOutcome failure_reason terminal_status committed snapshot	src/mindroom/response_terminal.py:42; src/mindroom/delivery_gateway.py:405; src/mindroom/delivery_gateway.py:1137
_raise_nonterminal_delivery_error	function	lines 134-136	none-found	_NonTerminalDeliveryError wrapper raise from error	none
_complete_capture_completions	function	lines 139-142	none-found	Future set_result capture_completions inflight capture	none
_format_stream_error_note	function	lines 145-152	none-found	Response interrupted by an error normalize split truncate	none
is_interrupted_partial_reply	function	lines 155-168	related-only	partial reply cancelled error interrupted markers clean_partial_reply	src/mindroom/response_runner.py:527; src/mindroom/execution_preparation.py:274; src/mindroom/matrix/stale_stream_cleanup.py:1183
clean_partial_reply_text	function	lines 171-190	related-only	strip partial reply markers progress placeholder visible tool markers	src/mindroom/response_runner.py:527; src/mindroom/history/storage.py:240
build_restart_interrupted_body	function	lines 193-198	related-only	Response interrupted by service restart stale stream cleanup reason	src/mindroom/matrix/stale_stream_cleanup.py:1226
_CommittedDeliveryState	class	lines 202-210	related-only	committed delivery state last delivered rendered body visible body state	src/mindroom/final_delivery.py:18; src/mindroom/response_terminal.py:14
_normalize_stream_accumulated_text	function	lines 213-215	none-found	normalize whitespace accumulated text placeholder empty state	none
build_cancelled_response_update	function	lines 218-234	related-only	cancel source user_stop sync_restart interrupted response note stream status	src/mindroom/orchestration/runtime.py:19; src/mindroom/response_terminal.py:42; src/mindroom/delivery_gateway.py:849
_log_stream_cancellation	function	lines 237-253	duplicate-found	log CancelledError cancel source sync_restart user_stop interrupted	src/mindroom/response_attempt.py:58; src/mindroom/response_runner.py:1185; src/mindroom/response_runner.py:1809; src/mindroom/response_runner.py:2012
_PreparedStreamingDelivery	class	lines 257-263	none-found	prepared streaming delivery committed_state had_warmup_suffix	none
StreamingResponse	class	lines 267-1002	related-only	streaming response Matrix send edit throttled accumulated text delivery	src/mindroom/streaming_delivery.py:153; src/mindroom/delivery_gateway.py:1053
StreamingResponse.__post_init__	method	lines 321-333	related-only	MessageTarget.resolve room_id thread_id reply_to_event_id room_mode	src/mindroom/delivery_gateway.py:520; src/mindroom/matrix/stale_stream_cleanup.py:1101
StreamingResponse._update	method	lines 335-337	none-found	append new chunk accumulated text	none
StreamingResponse.uses_replacement_updates	method	lines 339-341	none-found	replacement updates streaming chunk replaces body	none
StreamingResponse._append_incremental_text	method	lines 343-347	related-only	accumulated_text chars_since_last_update last_delta_at append	src/mindroom/streaming_delivery.py:187; src/mindroom/tool_system/events.py:406
StreamingResponse._mark_nonadditive_text_mutation	method	lines 349-352	related-only	nonadditive mutation chars_since_last_update last_delta_at	src/mindroom/streaming_delivery.py:287
StreamingResponse._ensure_hidden_tool_gap	method	lines 354-357	related-only	hidden tool gap accumulated_text double newline	src/mindroom/streaming_delivery.py:289; src/mindroom/tool_system/events.py:425
StreamingResponse._current_update_interval	method	lines 359-374	none-found	update interval ramp min_update_interval interval_ramp_seconds	none
StreamingResponse._current_char_threshold	method	lines 376-389	none-found	char threshold ramp min_update_char_threshold interval_ramp_seconds	none
StreamingResponse._mark_nonterminal_delivery	method	lines 391-416	related-only	nonterminal delivery committed state chars_since_last_update boundary refresh	src/mindroom/streaming_delivery.py:438; src/mindroom/streaming_delivery.py:475
StreamingResponse.matching_inflight_nonterminal_capture	method	lines 418-428	none-found	inflight nonterminal capture committed state matches live state	none
StreamingResponse._throttled_send	async_method	lines 430-466	none-found	throttled send time char idle trigger progress hint	none
StreamingResponse.update_content	async_method	lines 468-473	none-found	update content clear warmup terminal failures throttled send	none
StreamingResponse._prepare_terminal_text_and_status	method	lines 475-501	related-only	terminal text status cancelled restart interrupted error note	src/mindroom/response_terminal.py:42; src/mindroom/delivery_gateway.py:849
StreamingResponse.finalize	async_method	lines 503-691	related-only	finalize stream terminal update StreamTransportOutcome terminal_update_failed	src/mindroom/response_terminal.py:42; src/mindroom/delivery_gateway.py:1137
StreamingResponse._send_or_edit_message	async_method	lines 693-722	related-only	send or edit prepared delivery Matrix content	src/mindroom/delivery_gateway.py:520; src/mindroom/delivery_gateway.py:589; src/mindroom/matrix/stale_stream_cleanup.py:1101
StreamingResponse._send_prepared_delivery	async_method	lines 724-777	related-only	send prepared delivery initial edit mark committed	src/mindroom/delivery_gateway.py:520; src/mindroom/delivery_gateway.py:589
StreamingResponse._should_send_prepared_nonterminal_edit	method	lines 779-794	related-only	build_edit_event_content should_send_oversized_nonterminal_streaming_edit	src/mindroom/matrix/large_messages.py:152; src/mindroom/matrix/cache/thread_writes.py:106
StreamingResponse._prepare_delivery	method	lines 796-858	duplicate-found	format_message_with_mentions stream status visible body warmup suffix	src/mindroom/delivery_gateway.py:531; src/mindroom/delivery_gateway.py:589; src/mindroom/matrix/stale_stream_cleanup.py:1120
StreamingResponse._mark_delivery_committed	method	lines 860-868	related-only	last delivered snapshot committed rendered body visible body state	src/mindroom/final_delivery.py:18
StreamingResponse._committed_terminal_snapshot	method	lines 870-881	related-only	committed terminal snapshot placeholder visible_body_state	src/mindroom/response_terminal.py:42; src/mindroom/final_delivery.py:18
StreamingResponse.restore_last_delivered_state	method	lines 883-888	none-found	restore last delivered accumulated text tool trace placeholder	none
StreamingResponse.apply_worker_progress_event	method	lines 890-892	none-found	WorkerProgressEvent warmup state apply_event	none
StreamingResponse._resolve_stream_status	method	lines 894-902	related-only	stream status pending streaming completed constants	src/mindroom/execution_preparation.py:274; src/mindroom/matrix/stale_stream_cleanup.py:1354
StreamingResponse._record_streaming_send	async_method	lines 904-908	duplicate-found	notify_outbound_message after send_message_result conversation_cache	src/mindroom/delivery_gateway.py:564; src/mindroom/thread_summary.py:424; src/mindroom/scheduling.py:883; src/mindroom/hooks/sender.py:90
StreamingResponse._record_streaming_edit	async_method	lines 910-919	duplicate-found	notify_outbound_message after edit_message_result conversation_cache	src/mindroom/delivery_gateway.py:617; src/mindroom/matrix/stale_stream_cleanup.py:1135; src/mindroom/custom_tools/matrix_conversation_operations.py:657
StreamingResponse._mark_first_visible_reply_if_needed	method	lines 921-924	related-only	mark_first_visible_reply accumulated text stream_update final placeholder	src/mindroom/response_attempt.py:97; src/mindroom/response_runner.py:1128; src/mindroom/response_runner.py:1871; src/mindroom/turn_controller.py:1240
StreamingResponse._send_initial_content	async_method	lines 926-937	duplicate-found	send_message_result notify cache visible callback first visible	src/mindroom/delivery_gateway.py:564; src/mindroom/thread_summary.py:424; src/mindroom/hooks/sender.py:90
StreamingResponse._edit_existing_content	async_method	lines 939-960	duplicate-found	edit_message_result notify cache first visible	src/mindroom/delivery_gateway.py:617; src/mindroom/matrix/stale_stream_cleanup.py:1135; src/mindroom/custom_tools/matrix_conversation_operations.py:657
StreamingResponse._send_content	async_method	lines 962-1002	related-only	send edit retry terminal streaming update logging	src/mindroom/delivery_gateway.py:520; src/mindroom/delivery_gateway.py:589
ReplacementStreamingResponse	class	lines 1005-1021	none-found	replacement streaming response full body replaces chunks	none
ReplacementStreamingResponse.uses_replacement_updates	method	lines 1013-1015	none-found	uses replacement updates returns true	none
ReplacementStreamingResponse._update	method	lines 1017-1021	related-only	replace accumulated_text chars_since_last_update last_delta_at	src/mindroom/streaming_delivery.py:187
send_streaming_response	async_function	lines 1024-1220	related-only	streaming response consume stream shutdown progress delivery cancellation cleanup	src/mindroom/streaming_delivery.py:384; src/mindroom/response_attempt.py:139; src/mindroom/delivery_gateway.py:1053
```

## Findings

### 1. Cancellation provenance logging is duplicated

`src/mindroom/streaming.py:237` logs an `asyncio.CancelledError` by classifying its source and choosing sync-restart, user-stop, or unexpected-interruption log behavior.
`src/mindroom/response_attempt.py:58` implements the same behavior as `log_cancelled_response`, and `src/mindroom/response_runner.py:1185`, `src/mindroom/response_runner.py:1809`, and `src/mindroom/response_runner.py:2012` already call that shared helper.

The behavior is functionally the same: both call `classify_cancel_source(exc)`, log restart and user-stop as info, and log generic interruption as warning with traceback.
The only differences to preserve are the message strings and logger object.

### 2. Matrix send/edit plus cache notification is repeated

`StreamingResponse._send_initial_content` at `src/mindroom/streaming.py:926` sends Matrix content, records `event_id`, optionally invokes `visible_event_id_callback`, notifies `conversation_cache`, marks first visible timing, and logs.
`StreamingResponse._edit_existing_content` at `src/mindroom/streaming.py:939` edits Matrix content, notifies `conversation_cache`, and marks first visible timing.

The same send/edit-and-cache pattern appears in `DeliveryGateway.send_text` at `src/mindroom/delivery_gateway.py:520`, `DeliveryGateway.edit_text` at `src/mindroom/delivery_gateway.py:589`, stale stream cleanup at `src/mindroom/matrix/stale_stream_cleanup.py:1135`, thread summaries at `src/mindroom/thread_summary.py:424`, scheduling at `src/mindroom/scheduling.py:883`, hooks at `src/mindroom/hooks/sender.py:90`, and custom Matrix operations at `src/mindroom/custom_tools/matrix_conversation_operations.py:657`.

The duplicated behavior is not content formatting; it is the delivery result handling after `send_message_result` or `edit_message_result` returns a delivered event.
Differences to preserve include first visible timing, `visible_event_id_callback`, logging level/message, and whether the cache is optional.

### 3. Streaming visible-body preservation is related to stale cleanup

`StreamingResponse._prepare_delivery` at `src/mindroom/streaming.py:796` stores `STREAM_VISIBLE_BODY_KEY` and `STREAM_WARMUP_SUFFIX_KEY` when a warmup suffix is appended to the displayed body.
`_edit_stale_message` at `src/mindroom/matrix/stale_stream_cleanup.py:1101` also preserves a canonical visible body while editing stream metadata and removes warmup-specific keys when appropriate.

Both flows protect the client-visible body from transport-only suffix/metadata effects, but the semantics differ.
Streaming adds a warmup suffix and records the unsuffixed body.
Stale cleanup removes preserved body metadata before building the terminal cleanup edit and then restores the canonical visible body when needed.
This is related behavior rather than a clear immediate dedupe target.

## Proposed Generalization

1. Replace `_log_stream_cancellation` with a thin call to `response_attempt.log_cancelled_response`, or move that helper to a neutral module such as `mindroom.cancellation_logging` to avoid coupling streaming to response attempts.
2. If send/edit duplication continues to grow, add a small helper near `mindroom.matrix.client_delivery`, for example `record_delivered_message(conversation_cache, room_id, delivered)`, and use it after both sends and edits.
3. Do not generalize streaming warmup visible-body preservation yet.
The behavior is related, but the call sites encode different lifecycle rules and a helper would likely hide important differences.

## Risk/Tests

Cancellation logging dedupe is low risk if tests assert the three cancellation sources: `sync_restart`, `user_stop`, and generic interruption.
Send/edit cache notification dedupe is moderate risk because call sites differ on optional cache handling, visible-event callbacks, first-visible timing, and logging.
Relevant tests would cover streaming initial send, streaming edit, final delivery send/edit, stale stream cleanup edit, and cache notification payloads.
No production code was edited for this audit.
