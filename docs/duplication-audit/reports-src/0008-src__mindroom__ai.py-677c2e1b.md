## Summary

Top duplication candidates in `src/mindroom/ai.py`:

1. Agent and team execution paths duplicate retry/run-id/cancellation/metadata lifecycle behavior across `src/mindroom/ai.py` and `src/mindroom/teams.py`.
2. Streaming tool tracking is implemented separately in agent streaming, team streaming, and delivery-side stream consumption.
3. Request-log context assembly has parallel agent/team/OpenAI-compatible variants that mostly differ by entity label and prompt rendering.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_append_additional_context	function	lines 108-113	duplicate-found	additional_context session_preamble render_system_enrichment_block	src/mindroom/teams.py:151; src/mindroom/agents.py:214
_compose_current_turn_prompt	function	lines 116-138	related-only	strip_user_turn_time_prefix build_memory_prompt_parts prompt_chunks	src/mindroom/memory/functions.py:430; src/mindroom/response_runner.py:212; src/mindroom/response_runner.py:260
_PreparedAgentRun	class	lines 142-158	related-only	PreparedMaterializedTeamExecution PreparedExecution prompt_text run_input	src/mindroom/teams.py:161; src/mindroom/execution_preparation.py:99
_PreparedAgentRun.prompt_text	method	lines 151-153	duplicate-found	render_prepared_messages_text prepared_prompt	src/mindroom/teams.py:168; src/mindroom/execution_preparation.py:108
_PreparedAgentRun.run_input	method	lines 156-158	related-only	copy_run_input mutable message list	src/mindroom/ai_runtime.py:65; src/mindroom/ai_runtime.py:75; src/mindroom/ai_runtime.py:86
_next_retry_run_id	function	lines 161-165	duplicate-found	_next_retry_run_id uuid4 retry run_id	src/mindroom/teams.py:191
_prompt_current_sender_id	function	lines 168-172	related-only	include_openai_compat_guidance current_sender_id	src/mindroom/api/openai_compat.py:1076; src/mindroom/api/openai_compat.py:1277; src/mindroom/teams.py:1627
_build_timing_scope	function	lines 175-186	none-found	timing_scope reply_to_event_id run_id session_id agent_name	none
_note_attempt_run_id	function	lines 189-192	duplicate-found	run_id_callback current_run_id attempt_run_id	src/mindroom/teams.py:1653; src/mindroom/teams.py:2254; src/mindroom/ai_runtime.py:325
_render_system_enrichment_context	function	lines 196-202	related-only	render_system_enrichment_block system_enrichment_items	src/mindroom/teams.py:1472; src/mindroom/response_runner.py:1363; src/mindroom/hooks/enrichment.py:38
_compute_compaction_token_breakdown	function	lines 206-213	related-only	compute_prompt_token_breakdown compaction_token_breakdown	src/mindroom/history/compaction.py:computed via imported function
_StreamingAttemptState	class	lines 217-238	duplicate-found	pending_tools completed_tools request_metric_totals streaming state	src/mindroom/teams.py:2121; src/mindroom/streaming_delivery.py:224
_PendingStreamingTool	class	lines 242-246	duplicate-found	Pending tool tool_call_id visible_tool_index trace_entry	src/mindroom/teams.py:157
_extract_response_content	function	lines 249-267	related-only	RunOutput content tools format_tool_combined response content	src/mindroom/teams.py:1148; src/mindroom/tool_system/events.py:375
_extract_replayable_response_text	function	lines 270-272	related-only	canonical assistant text show_tool_calls false	src/mindroom/history/interrupted_replay.py:140; src/mindroom/teams.py:2445
_extract_tool_trace	function	lines 275-286	duplicate-found	extract tool trace RunOutput tools format_tool_combined format_tool_completed_event	src/mindroom/teams.py:1163; src/mindroom/api/openai_compat.py:1189
_extract_cancelled_tool_trace	function	lines 289-291	related-only	split_interrupted_tool_trace cancelled tool trace	src/mindroom/teams.py:1173; src/mindroom/history/interrupted_replay.py:102
_find_matching_pending_stream_tool	function	lines 294-312	duplicate-found	find pending tool call_id tool_name newest pending	src/mindroom/teams.py:2111; src/mindroom/streaming_delivery.py:276
_stream_attempt_has_progress	function	lines 315-317	duplicate-found	emitted_output pending_tools completed_tools retry progress	src/mindroom/teams.py:2309; src/mindroom/teams.py:2351
_is_run_cancelled_boilerplate	function	lines 320-323	duplicate-found	cancellation boilerplate startswith run cancel	src/mindroom/teams.py:1186
_extract_interrupted_partial_text	function	lines 326-356	duplicate-found	interrupted partial text cancellation boilerplate response content	src/mindroom/teams.py:1148
_raise_agent_run_cancelled	function	lines 359-361	duplicate-found	build_cancelled_error raise cancelled	src/mindroom/teams.py:1199; src/mindroom/cancellation.py:31
_normalized_string_list	function	lines 364-371	related-only	normalized string list metadata seen event ids	src/mindroom/history/interrupted_replay.py:222; src/mindroom/teams.py:1139
build_matrix_run_metadata	function	lines 374-423	related-only	matrix run metadata seen event ids source prompts correlation tools_schema	src/mindroom/history/interrupted_replay.py:153; src/mindroom/turn_store.py:197; src/mindroom/response_runner.py:435
resolve_run_correlation_id	function	lines 426-440	related-only	correlation_id metadata reply_to_event_id uuid4	src/mindroom/api/openai_compat.py:268; src/mindroom/tool_system/runtime_context.py:566; src/mindroom/history/compaction.py:242
_request_stream_retry	function	lines 443-464	duplicate-found	should_retry_without_inline_media retry_requested stream progress	src/mindroom/teams.py:2309; src/mindroom/teams.py:2351
_track_stream_tool_started	function	lines 467-491	duplicate-found	format_tool_started_event pending tool visible_tool_index	src/mindroom/teams.py:2135; src/mindroom/streaming_delivery.py:255
_track_stream_tool_completed	function	lines 494-526	duplicate-found	format_tool_completed_event complete_pending_tool_block missing pending tool start	src/mindroom/teams.py:2165; src/mindroom/streaming_delivery.py:270
_track_model_request_metrics	function	lines 529-564	none-found	ModelRequestCompletedEvent input_tokens cache_read_tokens time_to_first_token	none
_stream_completed_without_visible_output	function	lines 567-569	related-only	completed without visible output final status error observed tool calls	src/mindroom/teams.py:2442
_metrics_comparison_payload	function	lines 572-578	related-only	Metrics to_dict metrics dict comparison	src/mindroom/ai_run_metadata.py:223
_usage_metric_int	function	lines 581-586	related-only	usage metric int metrics payload input_tokens	src/mindroom/ai_run_metadata.py:262; src/mindroom/ai_run_metadata.py:281
_request_metrics_are_more_complete	function	lines 589-600	none-found	request metrics more complete completed metrics input output total	none
_select_streaming_usage_metrics	function	lines 603-611	none-found	select streaming usage metrics fallback completed_metrics	none
_attempt_request_log_context	function	lines 614-641	duplicate-found	build_llm_request_log_context full_prompt request log context	src/mindroom/teams.py:113; src/mindroom/api/openai_compat.py:258
_run_cached_agent_attempt	async_function	lines 645-668	related-only	cached_agent_run timed model_request_to_completion	src/mindroom/thread_summary.py:347; src/mindroom/topic_generator.py:101; src/mindroom/ai_runtime.py:328
_assert_agent_target	function	lines 671-679	related-only	configured team not agent assert team agents supported	src/mindroom/teams.py:1543; src/mindroom/api/openai_compat.py:openai model validation
_current_sender_id_kwargs	function	lines 682-695	related-only	include_openai_compat_guidance current_sender_id kwargs	src/mindroom/api/openai_compat.py:1463; src/mindroom/api/openai_compat.py:1542
_mark_pipeline_timing	function	lines 698-701	related-only	pipeline_timing mark if not None	src/mindroom/teams.py:1500; src/mindroom/response_runner.py:887; src/mindroom/execution_preparation.py:724
_prepare_agent_and_prompt	async_function	lines 705-841	related-only	prepare execution context build agent memory prompt additional context metadata prepared history	src/mindroom/teams.py:1450; src/mindroom/execution_preparation.py:752
ai_response	async_function	lines 844-1221	duplicate-found	non streaming response retry media metadata cancellation turn_recorder cleanup	src/mindroom/teams.py:1521
_process_stream_events	async_function	lines 1225-1333	duplicate-found	stream events content tool start complete error retry cancellation	src/mindroom/teams.py:2281; src/mindroom/streaming_delivery.py:224
stream_agent_response	async_function	lines 1336-1774	duplicate-found	streaming response retry media metadata cancellation turn_recorder cleanup	src/mindroom/teams.py:1864
stream_agent_response.<locals>._sync_live_turn_recorder	nested_function	lines 1527-1535	duplicate-found	sync_partial_state assistant_text completed_tools interrupted_tools	src/mindroom/teams.py:2101
```

## Findings

### 1. Agent and team execution lifecycles repeat the same retry/cancel/metadata shape

`ai_response` and `stream_agent_response` in `src/mindroom/ai.py:844` and `src/mindroom/ai.py:1336` repeat substantial lifecycle behavior that also exists in `team_response` and `stream_team_response` in `src/mindroom/teams.py:1521` and `src/mindroom/teams.py:1864`.
The duplicated behavior is not line-identical, but the functional flow matches:

- Resolve correlation id, media inputs, scoped session context, and run metadata.
- Prepare prompt/run input and install queued notice hooks.
- Try the original request, then optionally retry without inline media before any visible output.
- Allocate a fresh retry run id when the caller supplied an original run id.
- Convert errors through `get_user_friendly_error_message`.
- Persist or record interrupted replay state and raise the canonical cancellation error.
- Clean queued-notice state and close runtime state DBs.

Differences to preserve:

- Agent execution uses `RunOutput`, `Agent`, `prepare_agent_execution_context`, and `ai_runtime.append_inline_media_fallback_to_run_input`.
- Team execution can handle `TeamRunOutput`, nested member responses, member/consensus formatting, and `append_inline_media_fallback_prompt`.
- Team code performs bound seen-event persistence in places where agent code relies on Matrix run metadata/history preparation.

### 2. Streaming tool tracking is duplicated across agent, team, and delivery layers

Agent streaming tracks pending tools in `_PendingStreamingTool`, `_find_matching_pending_stream_tool`, `_track_stream_tool_started`, and `_track_stream_tool_completed` in `src/mindroom/ai.py:242`, `src/mindroom/ai.py:294`, `src/mindroom/ai.py:467`, and `src/mindroom/ai.py:494`.
Team streaming has the same behavior with a scope key in `_PendingTeamTool` and nested `_find_pending_tool_index`, `_start_tool`, and `_complete_tool` in `src/mindroom/teams.py:157`, `src/mindroom/teams.py:2111`, `src/mindroom/teams.py:2135`, and `src/mindroom/teams.py:2165`.
Delivery-side streaming repeats a simpler version in `_consume_streaming_chunks` at `src/mindroom/streaming_delivery.py:224`, including pending tool matching, visible marker completion, and trace mutation.

Differences to preserve:

- Agent streaming does not need a scope key.
- Team streaming needs per-member/team scope isolation and updates either member text or consensus text.
- Delivery-side streaming mutates the UI delivery accumulator and flush/queue behavior, so it should not own run-level interrupted trace state.

### 3. Small cancellation and retry helpers are duplicated between agent and team paths

`_next_retry_run_id` in `src/mindroom/ai.py:161` is duplicated by `src/mindroom/teams.py:191`.
`_is_run_cancelled_boilerplate` in `src/mindroom/ai.py:320` is duplicated by `_is_cancellation_boilerplate` in `src/mindroom/teams.py:1186`.
`_raise_agent_run_cancelled` in `src/mindroom/ai.py:359` is duplicated by `_raise_team_run_cancelled` in `src/mindroom/teams.py:1199`.
The partial-text extraction functions in `src/mindroom/ai.py:326` and `src/mindroom/teams.py:1148` share the same cancellation-boilerplate stripping rule, though team extraction must also format nested team/member output.

Differences to preserve:

- Team partial extraction calls `format_team_response` for `TeamRunOutput`.
- Agent partial extraction prefers non-history assistant messages before falling back to content.

### 4. Request-log context construction has parallel wrappers

`_attempt_request_log_context` in `src/mindroom/ai.py:614`, `_team_request_log_context` in `src/mindroom/teams.py:113`, and `_openai_team_request_log_context` in `src/mindroom/api/openai_compat.py:258` all call `build_llm_request_log_context` with the same metadata, requester, correlation, prompt, and rendered full-prompt shape.

Differences to preserve:

- Agent attempts render `ai_runtime.ModelRunInput` through a copied message list.
- Team requests may receive either `str` or `list[Message]`.
- OpenAI-compatible team requests intentionally use `agent_id=f"team/{team_name}"`, no Matrix room/thread ids, and generate a fallback correlation id locally.

### 5. `_append_additional_context` is duplicated directly

`_append_additional_context` in `src/mindroom/ai.py:108` and `src/mindroom/teams.py:151` implement the same append-with-blank-line behavior for objects with `additional_context`.
This is a small but real duplicate.
The behavior is also related to, but not the same as, context rendering in `src/mindroom/agents.py:214`.

## Proposed Generalization

1. Move the tiny shared cancellation/retry helpers to a focused runtime helper module, likely `src/mindroom/agent_run_context.py` or `src/mindroom/cancellation.py` depending on ownership: `next_retry_run_id`, `is_cancellation_boilerplate`, and `raise_run_cancelled`.
2. Extract a small streaming tool tracker that stores pending tool trace entries and exposes `start(tool, scope_key=None)` and `complete(tool, scope_key=None)`.
The helper should return visible text/trace deltas without knowing about Matrix delivery queues or team member formatting.
3. Extract request-log context rendering into one helper that accepts `entity_id`, correlation fields, metadata, and a callable or value for full-prompt rendering.
4. Leave full `ai_response`/`team_response` lifecycle unmerged for now.
The overlap is real, but the behavior differences are large enough that a broad runner abstraction would be risky without first extracting the smaller helpers above.
5. Optionally move `append_additional_context` to a tiny utility only if touching both agent and team prompt-preparation code for another reason.

## Risk/tests

No production code was changed.

If the proposed refactors are implemented later, tests should cover:

- Agent non-stream retry without inline media, including fresh retry run id and no retry after visible progress.
- Team non-stream retry without inline media, including prompt fallback and notice-state cleanup.
- Agent and team cancellation partial-text extraction, including Agno cancellation boilerplate.
- Agent and team streaming tool start/complete matching by call id and by tool name fallback.
- Hidden-tool-call streaming where trace is recorded but visible markers are not emitted.
- Request-log context payloads for agent, configured team, and OpenAI-compatible team paths.
