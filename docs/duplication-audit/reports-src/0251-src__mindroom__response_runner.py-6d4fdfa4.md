Summary: The strongest duplication candidates are response pipeline setup repeated between non-streaming and streaming agent paths, team and agent terminal failure finalization, and the matrix messaging-tool probe duplicated in `bot.py`.
The rest of `response_runner.py` is mostly lifecycle orchestration or typed carriers with related collaborators but no clear cross-module duplicate worth extracting.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_merge_response_extra_content	function	lines 109-117	related-only	ATTACHMENT_IDS_KEY extra_content tool_trace extra_content	 src/mindroom/delivery_gateway.py:78, src/mindroom/final_delivery.py:49, src/mindroom/response_runner.py:1117
_split_delivery_tool_trace	function	lines 120-131	none-found	tool_call_completed interrupted_tools completed_tools	none
_strip_visible_tool_markers	function	lines 134-160	related-only	visible tool markers tool trace clean_partial_reply_text separator	src/mindroom/streaming.py:547, src/mindroom/response_runner.py:531
_materialize_matrix_run_metadata	function	lines 163-169	none-found	matrix_run_metadata dict Mapping metadata collector	none
_agent_has_matrix_messaging_tool	function	lines 172-178	duplicate-found	get_agent_tools matrix_message ValueError	src/mindroom/bot.py:1512
_append_matrix_prompt_context	function	lines 181-202	none-found	Matrix metadata for tool calls room_id thread_id reply_to_event_id	none
_prefix_user_turn_time	function	lines 205-217	related-only	strip_user_turn_time_prefix timezone timestamp user turn	src/mindroom/memory/functions.py:strip_user_turn_time_prefix
_timestamp_thread_history_user_turns	function	lines 220-244	none-found	ORIGINAL_SENDER_KEY is_agent_id replace_visible_message timestamp	none
prepare_memory_and_model_context	function	lines 247-279	related-only	prepare_memory_and_model_context timestamp model_prompt memory_prompt	src/mindroom/bot.py:1865, src/mindroom/response_runner.py:891, src/mindroom/response_runner.py:2112
_should_strip_transient_enrichment	function	lines 282-290	related-only	strip_transient_enrichment_after_run model_prompt transient tail	src/mindroom/turn_policy.py:166, src/mindroom/post_response_effects.py:249
_model_prompt_has_transient_tail	function	lines 293-299	related-only	model_prompt transient tail strip_user_turn_time_prefix	src/mindroom/turn_policy.py:180
ResponseRequest	class	lines 303-326	not-a-behavior-symbol	ResponseRequest dataclass fields	none
PostLockRequestPreparationError	class	lines 329-330	not-a-behavior-symbol	PostLockRequestPreparationError RuntimeError	none
TeamResponseRequest	class	lines 334-340	not-a-behavior-symbol	TeamResponseRequest dataclass fields	none
ResponseRunnerDeps	class	lines 344-359	not-a-behavior-symbol	ResponseRunnerDeps dataclass collaborators	none
_PreparedResponseRuntime	class	lines 363-372	not-a-behavior-symbol	_PreparedResponseRuntime dataclass fields	none
ResponseRunner	class	lines 376-2444	related-only	ResponseRunner lifecycle generate_response process_and_respond	ResponseAttemptRunner in src/mindroom/response_attempt.py:78, ResponseLifecycle in src/mindroom/response_lifecycle.py:286
ResponseRunner._client	method	lines 386-392	related-only	Matrix client is not ready _client	src/mindroom/delivery_gateway.py:335, src/mindroom/post_response_effects.py:82
ResponseRunner._log_delivery_failure	method	lines 394-406	related-only	Error in response delivery failure_reason error_type	src/mindroom/response_lifecycle.py:302
ResponseRunner.in_flight_response_count	method	lines 409-411; lines 414-416	not-a-behavior-symbol	in_flight_response_count property; in_flight_response_count setter	none
ResponseRunner._show_tool_calls	method	lines 418-423	related-only	show_tool_calls_for_agent show_tool_calls	src/mindroom/agents.py, src/mindroom/response_runner.py:920
ResponseRunner._build_turn_recorder	method	lines 425-441	none-found	TurnRecorder build_matrix_run_metadata set_run_metadata	none
ResponseRunner._persist_interrupted_turn	method	lines 443-471	related-only	persist_interrupted_replay_snapshot claim_interrupted_persistence create_storage	src/mindroom/history/interrupted_replay.py, src/mindroom/response_runner.py:478
ResponseRunner._ensure_recorder_interrupted	method	lines 473-476	none-found	mark_interrupted recorder outcome interrupted	none
ResponseRunner._persist_interrupted_recorder	method	lines 478-499	related-only	persist interrupted recorder cancellation	src/mindroom/response_runner.py:1093, src/mindroom/response_runner.py:1605, src/mindroom/response_runner.py:1719
ResponseRunner._strip_transient_enrichment_from_session	method	lines 501-521	related-only	strip_transient_enrichment_from_session create_storage close	src/mindroom/post_response_effects.py:249, src/mindroom/history/storage.py:147
ResponseRunner._record_stream_delivery_error	method	lines 523-547	none-found	record_interrupted clean_partial_reply_text interrupted_tools	none
ResponseRunner.has_active_response_for_target	method	lines 549-551	related-only	has_active_response_for_target lifecycle coordinator	src/mindroom/response_lifecycle.py:99
ResponseRunner.reserve_waiting_human_message	method	lines 553-563	related-only	reserve_waiting_human_message queued human notice	src/mindroom/response_lifecycle.py:138
ResponseRunner._run_in_tool_context	async_method	lines 565-578	related-only	run_in_context runtime_context_from_dispatch_context execution_identity	src/mindroom/tool_system/runtime_context.py, src/mindroom/tool_system/worker_routing.py
ResponseRunner._stream_in_tool_context	method	lines 580-593	related-only	stream_in_context runtime_context_from_dispatch_context execution_identity	src/mindroom/tool_system/runtime_context.py, src/mindroom/tool_system/worker_routing.py
ResponseRunner._resolve_request_target	method	lines 595-605	related-only	build_message_target response_envelope target	src/mindroom/bot.py:1574, src/mindroom/response_attempt.py:103
ResponseRunner._active_response_event_ids	method	lines 607-613	none-found	tracked_messages active_event_ids room_id task.done	none
ResponseRunner._run_locked_response_lifecycle	async_method	lines 615-629	related-only	run_locked_response queued_notice_reservation pipeline_timing	src/mindroom/response_lifecycle.py:188
ResponseRunner._build_persist_response_event_id_effect	method	lines 631-653	related-only	persist_response_event_id_in_session_run storage close	src/mindroom/post_response_effects.py:230, src/mindroom/response_runner.py:1001, src/mindroom/response_runner.py:2201
ResponseRunner._build_persist_response_event_id_effect.<locals>.persist_response_event_id	nested_function	lines 640-651	related-only	persist_response_event_id storage close	src/mindroom/post_response_effects.py:230
ResponseRunner._request_for_delivery	method	lines 655-666	none-found	existing_event_id existing_event_is_placeholder replace request delivery	none
ResponseRunner._build_compaction_lifecycle	method	lines 668-684	related-only	MatrixCompactionLifecycle reply_to_event_id existing_event_is_placeholder	src/mindroom/delivery_gateway.py:249
ResponseRunner._refresh_thread_history_after_lock	async_method	lines 686-711	related-only	fetch_thread_history requires_full_thread_history caller_label	src/mindroom/turn_controller.py:1317
ResponseRunner._prepare_request_after_lock	async_method	lines 713-724	none-found	prepare_after_lock PostLockRequestPreparationError	none
ResponseRunner._note_pipeline_metadata	method	lines 726-739	related-only	pipeline_timing note response_kind used_streaming	src/mindroom/turn_controller.py:1317
ResponseRunner._response_envelope_for_request	method	lines 741-767	related-only	MessageEnvelope source_event_id requester_id sender_id body	src/mindroom/turn_controller.py:1200, src/mindroom/hooks.py
ResponseRunner._correlation_id_for_request	method	lines 769-771	none-found	correlation_id reply_to_event_id	none
ResponseRunner._build_lifecycle	method	lines 773-798	related-only	ResponseLifecycle ResponseLifecycleDeps response_hooks logger	src/mindroom/response_lifecycle.py:286
ResponseRunner._finalize_empty_prompt_locked	async_method	lines 800-828	related-only	cancelled_for_empty_prompt lifecycle.finalize ResponseOutcome	src/mindroom/final_delivery.py:78
ResponseRunner.generate_team_response_helper	async_method	lines 830-851	related-only	TeamResponseRequest run_locked_response_lifecycle	src/mindroom/bot.py:1522
ResponseRunner.generate_response_for_empty_prompt	async_method	lines 853-867	related-only	empty prompt response_kind run_locked_response_lifecycle	src/mindroom/bot.py:1883
ResponseRunner.generate_team_response_helper_locked	async_method	lines 869-1395	duplicate-found	team response streaming non-streaming finalize cancellation session watch	src/mindroom/response_runner.py:1731, src/mindroom/response_runner.py:1879, src/mindroom/response_runner.py:2091
ResponseRunner.generate_team_response_helper_locked.<locals>.team_storage_factory	nested_function	lines 969-970	duplicate-found	create_storage execution_identity scope session_scope	src/mindroom/response_runner.py:1766, src/mindroom/response_runner.py:1916
ResponseRunner.generate_team_response_helper_locked.<locals>.generate_team_response	nested_async_function	lines 1007-1246	duplicate-found	typed indicator run in tool context deliver_final deliver_stream finalize streamed	src/mindroom/response_runner.py:1527, src/mindroom/response_runner.py:1618, src/mindroom/response_runner.py:2218
ResponseRunner.generate_team_response_helper_locked.<locals>._note_attempt_run_id	nested_function	lines 1022-1024	duplicate-found	update_run_id set_run_id attempt_run_id	src/mindroom/response_runner.py:1548, src/mindroom/response_runner.py:1639
ResponseRunner.generate_team_response_helper_locked.<locals>._note_visible_response_event_id	nested_function	lines 1026-1029	related-only	set_response_event_id visible_event_id_callback	src/mindroom/response_runner.py:1644
ResponseRunner.generate_team_response_helper_locked.<locals>.build_response_stream	nested_function	lines 1037-1067	related-only	team_response_stream parameters stream_agent_response parameters	src/mindroom/response_runner.py:1657, src/mindroom/teams.py
ResponseRunner.generate_team_response_helper_locked.<locals>.build_response_text	nested_async_function	lines 1135-1164	related-only	team_response parameters ai_response parameters	src/mindroom/response_runner.py:1553, src/mindroom/teams.py
ResponseRunner.generate_team_response_helper_locked.<locals>.note_task_cancelled	nested_function	lines 1255-1258	duplicate-found	delivery_failure_reason delivery_cancelled on_cancelled	src/mindroom/response_runner.py:2213
ResponseRunner.run_cancellable_response	async_method	lines 1397-1446	related-only	ResponseAttemptRunner ResponseAttemptRequest in_flight_response_count	src/mindroom/response_attempt.py:78
ResponseRunner._prepare_response_runtime_common	async_method	lines 1448-1492	related-only	resolve target thread root MediaInputs build_dispatch_context	src/mindroom/response_runner.py:891, src/mindroom/response_runner.py:2112
ResponseRunner.prepare_non_streaming_runtime	async_method	lines 1495-1504	related-only	prepare runtime existing_event_uses_thread_id room_mode	src/mindroom/response_runner.py:1507
ResponseRunner.prepare_streaming_runtime	async_method	lines 1507-1524	related-only	prepare runtime get_entity_thread_mode room mode	src/mindroom/response_runner.py:1495
ResponseRunner.generate_non_streaming_ai_response	async_method	lines 1527-1615	duplicate-found	knowledge enrichment ai call typing interrupted recorder	src/mindroom/response_runner.py:1618
ResponseRunner.generate_non_streaming_ai_response.<locals>.note_attempt_run_id	nested_function	lines 1548-1551	duplicate-found	update_run_id set_run_id append attempt_run_id	src/mindroom/response_runner.py:1639, src/mindroom/response_runner.py:1022
ResponseRunner.generate_non_streaming_ai_response.<locals>.build_response_text	nested_async_function	lines 1553-1597	related-only	ai_response stream_agent_response shared parameters	src/mindroom/response_runner.py:1657, src/mindroom/ai.py
ResponseRunner.generate_streaming_ai_response	async_method	lines 1618-1729	duplicate-found	knowledge enrichment streaming ai call typing interrupted recorder	src/mindroom/response_runner.py:1527
ResponseRunner.generate_streaming_ai_response.<locals>.note_attempt_run_id	nested_function	lines 1639-1642	duplicate-found	update_run_id set_run_id append attempt_run_id	src/mindroom/response_runner.py:1548, src/mindroom/response_runner.py:1022
ResponseRunner.generate_streaming_ai_response.<locals>.note_visible_response_event_id	nested_function	lines 1644-1645	related-only	set_response_event_id visible_event_id_callback	src/mindroom/response_runner.py:1026
ResponseRunner.process_and_respond	async_method	lines 1731-1877	duplicate-found	response runtime setup lifecycle session watch collectors cancelled note final delivery	src/mindroom/response_runner.py:1879
ResponseRunner.process_and_respond.<locals>.history_storage_factory	nested_function	lines 1766-1767	duplicate-found	create_storage execution_identity scope session_scope	src/mindroom/response_runner.py:1916, src/mindroom/response_runner.py:969
ResponseRunner.process_and_respond_streaming	async_method	lines 1879-2079	duplicate-found	response runtime setup lifecycle session watch collectors stream error finalization	src/mindroom/response_runner.py:1731
ResponseRunner.process_and_respond_streaming.<locals>.history_storage_factory	nested_function	lines 1916-1917	duplicate-found	create_storage execution_identity scope session_scope	src/mindroom/response_runner.py:1766, src/mindroom/response_runner.py:969
ResponseRunner.generate_response	async_method	lines 2081-2089	related-only	run_locked_response_lifecycle generate response helper	src/mindroom/response_runner.py:830
ResponseRunner.generate_response_locked	async_method	lines 2091-2444	duplicate-found	response lifecycle terminal failure handling cancellation finalization team path	src/mindroom/response_runner.py:869
ResponseRunner.generate_response_locked.<locals>.queue_memory_persistence	nested_function	lines 2176-2199	related-only	mark_auto_flush_dirty_session store_conversation_memory create_background_task	src/mindroom/post_response_effects.py:260, src/mindroom/memory/functions.py
ResponseRunner.generate_response_locked.<locals>.note_delivery_started	nested_function	lines 2207-2211	none-found	delivery_stage_started tracked_event_id on_delivery_started	none
ResponseRunner.generate_response_locked.<locals>.note_task_cancelled	nested_function	lines 2213-2216	duplicate-found	delivery_failure_reason delivery_cancelled on_cancelled	src/mindroom/response_runner.py:1255
ResponseRunner.generate_response_locked.<locals>.generate	nested_async_function	lines 2218-2246	related-only	use_streaming process_and_respond_streaming process_and_respond collectors	src/mindroom/response_runner.py:1007
```

Findings:

1. Matrix messaging tool detection is duplicated.
`src/mindroom/response_runner.py:172` and `src/mindroom/bot.py:1512` both ask config for agent tools, treat `ValueError` as no access, and test for `"matrix_message"`.
The `bot.py` copy also guards for non-list containers, while the `response_runner.py` copy trusts `get_agent_tools`.
This is functionally the same capability check and should have one source of truth if both call sites remain.

2. Agent non-streaming and streaming response paths duplicate runtime/lifecycle/session-watch scaffolding.
`src/mindroom/response_runner.py:1731` and `src/mindroom/response_runner.py:1879` both mark response runtime timing, prepare runtime, collect the model prompt, build response envelope/correlation/lifecycle, create the same history storage factory, setup `session_started_watch`, initialize collectors, build a turn recorder, emit session started in a `finally`, merge response extra content, finalize delivery, mark first visible reply/response completion, and append run/compaction results.
The differences to preserve are the runtime preparation method, generation method, `StreamingDeliveryError` handling, final delivery request type, and streaming-specific collectors.

3. AI response generation duplicates enrichment and run-id callback setup between streaming and non-streaming.
`src/mindroom/response_runner.py:1527` and `src/mindroom/response_runner.py:1618` both build a compaction lifecycle, define the same `note_attempt_run_id` callback, resolve knowledge, append knowledge availability enrichment, render transient system context, materialize matrix run metadata, pass nearly identical arguments into `ai_response`/`stream_agent_response`, wrap execution with typing/tool context, and persist interrupted recorder snapshots on cancellation.
The differences are response function (`ai_response` vs `stream_agent_response`), stream delivery after generation, and tool trace collector support.

4. Team and agent terminal failure handling repeat the same placeholder-terminal outcome construction.
`src/mindroom/response_runner.py:1331`, `src/mindroom/response_runner.py:2021`, and `src/mindroom/response_runner.py:2268` all convert a late error/cancellation plus pending visible event IDs into a `FinalizeStreamedResponseRequest`.
Two paths already use `PendingVisibleResponse` and `build_terminal_stream_transport_outcome`; the agent pre-delivery cancellation/error branches manually build equivalent `StreamTransportOutcome` objects at `src/mindroom/response_runner.py:2280`, `src/mindroom/response_runner.py:2317`, and `src/mindroom/response_runner.py:2353`.
The differences to preserve are whether there is a real `run_message_id`, whether an exception should be re-raised after lifecycle finalization, and whether extra content/tool trace is available.

5. Session storage factory and session-start watch setup are duplicated across team, non-streaming agent, and streaming agent paths.
`src/mindroom/response_runner.py:969`, `src/mindroom/response_runner.py:1766`, and `src/mindroom/response_runner.py:1916` each close over an execution identity and `HistoryScope` to call `state_writer.create_storage`.
The following `lifecycle.setup_session_watch(...)` call is also repeated with the same shape at `src/mindroom/response_runner.py:972`, `src/mindroom/response_runner.py:1769`, and `src/mindroom/response_runner.py:1919`.
The only real differences are team vs agent scope and the source of the resolved thread id.

Proposed generalization:

1. Move the matrix messaging-tool capability check into a small helper near config/tool policy, then call it from both `bot.py` and `response_runner.py`.
2. Add a private response-run setup helper in `response_runner.py` that returns runtime, response envelope, correlation id, lifecycle, history scope/type, session-start watch, collectors, active IDs, and turn recorder for agent paths.
3. Add a private AI-call preparation helper that resolves knowledge, appends enrichment, records transient system context, materializes matrix metadata, and returns the common kwargs for `ai_response`/`stream_agent_response`.
4. Replace the manual agent placeholder `StreamTransportOutcome(...)` branches with `build_terminal_stream_transport_outcome(PendingVisibleResponse(...))`.
5. Keep team refactoring separate unless the agent path extraction proves clean; team behavior has more differences around `TeamMode`, team storage scope, and `team_response_stream`.

Risk/tests:

Any refactor here is high risk because this module is the visible response lifecycle boundary.
Tests should cover non-streaming success, streaming success, `StreamingDeliveryError`, user cancellation before delivery starts, user cancellation after a placeholder exists, sync-restart cancellation, existing-event edit mode, placeholder adoption, empty prompt finalization, response event-id persistence, and transient enrichment cleanup.
No production code was edited for this audit.
