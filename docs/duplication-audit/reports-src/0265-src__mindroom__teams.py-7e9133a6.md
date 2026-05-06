## Summary

Top duplication candidates:

1. Team execution preparation is duplicated between `src/mindroom/teams.py` and the OpenAI-compatible API team path in `src/mindroom/api/openai_compat.py`.
2. Team streaming tool tracking repeats the pending-tool matching, start, completion, trace update, and no-progress retry policy used by agent streaming in `src/mindroom/ai.py`.
3. Team non-streaming and streaming response loops mirror the agent response loops in `src/mindroom/ai.py` for inline-media fallback retries, queued-notice cleanup, cancellation recording, and friendly error conversion.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_team_run_input_text	function	lines 105-108	related-only	request log run_input render_prepared_messages_text	src/mindroom/ai.py:374, src/mindroom/execution_preparation.py:426
_team_request_log_context	function	lines 111-136	related-only	build_llm_request_log_context attempt_request_log_context	src/mindroom/ai.py:1000, src/mindroom/llm_request_logging.py
_append_additional_context	function	lines 151-153	none-found	additional_context append system_enrichment	none
_PendingTeamTool	class	lines 157-162	duplicate-found	PendingStreamingTool pending tool trace visible_tool_index	src/mindroom/ai.py:240, src/mindroom/ai.py:294
TeamMode	class	lines 165-169	related-only	TeamMode coordinate collaborate delegate_to_all_members	src/mindroom/response_runner.py:913
_PreparedMaterializedTeamExecution	class	lines 173-188	duplicate-found	PreparedOpenAIMaterializedTeamExecution prepared team execution	src/mindroom/api/openai_compat.py:1473, src/mindroom/api/openai_compat.py:1552
_PreparedMaterializedTeamExecution.prepared_prompt	method	lines 181-183	duplicate-found	render_prepared_team_messages_text prepared messages	src/mindroom/api/openai_compat.py:1552
_PreparedMaterializedTeamExecution.context_messages	method	lines 186-188	none-found	context_messages messages slice team execution	none
_next_retry_run_id	function	lines 191-195	related-only	next_retry_run_id uuid4 retry run id	src/mindroom/ai.py:1090, src/mindroom/ai.py:1600
_TeamModeDecision	class	lines 198-204	none-found	TeamModeDecision output_schema mode reasoning	none
_format_team_header	function	lines 207-217	none-found	Team Response header format	none
_format_member_contribution	function	lines 220-233	none-found	member contribution bold agent name	none
_format_team_consensus	function	lines 236-252	none-found	Team Consensus formatting	none
_format_no_consensus_note	function	lines 255-266	none-found	No team consensus formatting	none
format_team_response	function	lines 269-281	related-only	format team output member_responses	src/mindroom/api/openai_compat.py:1439
is_errored_run_output	function	lines 284-287	related-only	RunStatus error status normalize	src/mindroom/ai.py:1082, src/mindroom/api/openai_compat.py:1447
is_cancelled_run_output	function	lines 290-293	related-only	RunStatus cancelled status normalize	src/mindroom/ai.py:1134, src/mindroom/api/openai_compat.py:1447
_team_response_text	function	lines 296-299	related-only	format team output fallback content	src/mindroom/api/openai_compat.py:1439
_format_terminal_team_response	function	lines 302-308	none-found	terminal team response header body	none
_cleanup_team_notice_state	function	lines 311-325	related-only	cleanup queued notice state session type	src/mindroom/ai.py:1105, src/mindroom/ai.py:1721
_scrub_team_retry_notice_state	function	lines 328-337	related-only	scrub queued notice retry state	src/mindroom/ai.py:966, src/mindroom/ai.py:1459
_format_contributions_recursive	function	lines 340-389	none-found	recursive member_responses nested team formatting	none
_get_response_content	function	lines 392-418	related-only	extract response content assistant messages	src/mindroom/ai.py:249, src/mindroom/ai.py:270
TeamIntent	class	lines 421-427	not-a-behavior-symbol	enum values only	none
TeamMemberStatus	class	lines 430-437	not-a-behavior-symbol	enum values only	none
TeamOutcome	class	lines 440-446	not-a-behavior-symbol	enum values only	none
TeamResolutionMember	class	lines 450-456	not-a-behavior-symbol	dataclass container only	none
TeamResolution	class	lines 460-562	none-found	TeamResolution outcome invariant factories	none
TeamResolution.__post_init__	method	lines 471-493	none-found	TeamResolution invariants outcome mode eligible	none
TeamResolution.none	method	lines 496-504	none-found	TeamResolution none factory	none
TeamResolution.reject	method	lines 507-524	none-found	TeamResolution reject factory eligible members	none
TeamResolution.team	method	lines 527-544	none-found	TeamResolution team factory	none
TeamResolution.individual	method	lines 547-562	none-found	TeamResolution individual factory	none
_SelectedTeamRequest	class	lines 566-570	not-a-behavior-symbol	dataclass container only	none
_select_team_mode	async_function	lines 573-638	none-found	TeamModeDecider prompt output_schema	none
decide_team_formation	async_function	lines 641-723	related-only	team formation selected request evaluate resolve mode	src/mindroom/turn_policy.py:397
_team_member_name	function	lines 726-734	related-only	MatrixID agent_name fallback username	src/mindroom/conversation_state_writer.py:58, src/mindroom/response_runner.py:915
_filter_team_request_members	function	lines 737-750	related-only	filter router non config agents agent_name	src/mindroom/thread_utils.py:133
_normalize_team_request_members	function	lines 753-766	duplicate-found	deduplicate MatrixID full_id preserve order	src/mindroom/thread_utils.py:262
_select_team_request	function	lines 769-824	none-found	select team request tagged mentioned thread dm	none
_sender_unavailable_team_agents_message	function	lines 827-835	related-only	agent not available to you message	src/mindroom/teams.py:1071
_mixed_unavailable_team_agents_message	function	lines 838-846	related-only	agent not available for this request message	src/mindroom/teams.py:1071
_room_unavailable_team_agents_message	function	lines 849-857	related-only	agent not available in this room message	src/mindroom/scheduling.py:1337, src/mindroom/teams.py:1071
_not_materializable_team_agents_message	function	lines 860-868	related-only	could not be materialized team request	src/mindroom/teams.py:1071
_evaluate_team_members	function	lines 871-929	related-only	unsupported team agents available agents room sender visibility	src/mindroom/authorization.py, src/mindroom/config/main.py:1478
_resolve_team_request	function	lines 932-984	none-found	team resolution outcome eligible rejected	none
_team_resolution_reason	function	lines 987-1032	related-only	unsupported team agent message not available reason	src/mindroom/agent_policy.py:293, src/mindroom/config/main.py:1500
_mixed_team_resolution_reason	function	lines 1035-1043	none-found	mixed team resolution per member details	none
_unsupported_team_member_detail	function	lines 1046-1068	related-only	unsupported private team member detail	src/mindroom/agent_policy.py:271, src/mindroom/agent_policy.py:293
_team_resolution_member_detail	function	lines 1071-1084	related-only	member status to reason fragment	src/mindroom/agent_policy.py:271
resolve_configured_team	function	lines 1087-1113	related-only	configured team resolve evaluate request	src/mindroom/config/main.py:1522
_persist_bound_seen_event_ids	function	lines 1116-1136	related-only	update_scope_seen_event_ids session metadata	src/mindroom/history/storage.py:286, src/mindroom/history/compaction.py:542
_run_metadata_seen_event_ids	function	lines 1139-1145	related-only	MATRIX_SEEN_EVENT_IDS_METADATA_KEY normalized list	src/mindroom/ai.py:349, src/mindroom/history/storage.py:277
_extract_interrupted_team_partial_text	function	lines 1148-1161	duplicate-found	interrupted partial text cancellation boilerplate	src/mindroom/ai.py:308
_extract_completed_team_tool_trace	function	lines 1164-1175	duplicate-found	extract tool trace RunOutput tools recursive	src/mindroom/ai.py:275, src/mindroom/history/storage.py:193
_extract_cancelled_team_tool_trace	function	lines 1178-1189	duplicate-found	cancelled tool trace split_interrupted_tool_trace recursive	src/mindroom/ai.py:289
_is_cancellation_boilerplate	function	lines 1192-1195	duplicate-found	run cancel boilerplate normalized string	src/mindroom/ai.py:303
_raise_team_run_cancelled	function	lines 1198-1200	related-only	raise canonical cancellation error	src/mindroom/ai.py:335
materialize_exact_team_members	function	lines 1203-1266	related-only	materialize exact requested team members create_agent knowledge access	src/mindroom/team_exact_members.py, src/mindroom/api/openai_compat.py:1430
materialize_exact_team_members.<locals>._build_member	nested_function	lines 1220-1250	related-only	build member create_agent resolve knowledge access	src/mindroom/agents.py:1248, src/mindroom/custom_tools/delegate.py:172
_requested_team_agent_names	function	lines 1269-1271	none-found	exclude router agent names	none
_materialize_team_members	function	lines 1274-1297	related-only	orchestrator materialize team members live shared agents	src/mindroom/api/openai_compat.py:1430
_create_team_instance	function	lines 1300-1372	related-only	create Agno Team history settings delegate_to_all_members	src/mindroom/history/runtime.py:1465, src/mindroom/api/openai_compat.py:1430
select_model_for_team	function	lines 1375-1410	related-only	resolve_runtime_model room model team model logging	src/mindroom/ai.py:753, src/mindroom/ai_run_metadata.py:44
build_materialized_team_instance	function	lines 1413-1441	related-only	build materialized Team instance resolve runtime model	src/mindroom/api/openai_compat.py:1430
_prepare_materialized_team_execution	async_function	lines 1444-1519	duplicate-found	prepare_bound_team_run_context build_matrix_run_metadata prepared team	src/mindroom/api/openai_compat.py:1473
team_response	async_function	lines 1522-1845	duplicate-found	nonstream response retry inline media cleanup cancellation record_completed	src/mindroom/ai.py:844, src/mindroom/ai.py:1065
team_response.<locals>._run	nested_async_function	lines 1647-1679	related-only	attach_media_to_run_input bind request log arun	src/mindroom/ai.py:1000, src/mindroom/ai.py:1563
_team_response_stream_raw	async_function	lines 1848-1906	related-only	raw stream arun stream_events fallback error event	src/mindroom/ai.py:1563
_team_response_stream_raw.<locals>._empty	nested_async_function	lines 1867-1868	none-found	empty no agents async iterator	none
_team_response_stream_raw.<locals>._start_stream	nested_function	lines 1881-1895	related-only	attach media start arun stream_events	src/mindroom/ai.py:1563
_team_response_stream_raw.<locals>._error	nested_async_function	lines 1903-1904	none-found	error async iterator TeamRunErrorEvent	none
team_response_stream	async_function	lines 1909-2491	duplicate-found	stream response retry event loop tool tracking recorder cleanup	src/mindroom/ai.py:1225, src/mindroom/ai.py:1366
team_response_stream.<locals>._empty_canonical_partial_text	nested_function	lines 1993-1994	none-found	empty canonical partial text	none
team_response_stream.<locals>._scope_key_for_agent	nested_function	lines 2057-2058	none-found	agent scope key string	none
team_response_stream.<locals>._get_visible_consensus	nested_function	lines 2060-2061	none-found	visible consensus getter closure	none
team_response_stream.<locals>._append_to_visible_consensus	nested_function	lines 2063-2065	none-found	visible consensus append closure	none
team_response_stream.<locals>._set_visible_consensus	nested_function	lines 2067-2069	none-found	visible consensus setter closure	none
team_response_stream.<locals>._render_team_parts	nested_function	lines 2071-2089	related-only	render member parts consensus no consensus	src/mindroom/teams.py:340
team_response_stream.<locals>._current_canonical_partial_text	nested_function	lines 2091-2097	related-only	render team parts canonical text join	src/mindroom/teams.py:296
team_response_stream.<locals>._sync_live_turn_recorder	nested_function	lines 2101-2107	duplicate-found	sync_partial_state completed interrupted pending tools	src/mindroom/ai.py:1515
team_response_stream.<locals>._find_pending_tool_index	nested_function	lines 2109-2132	duplicate-found	find pending tool call_id fallback tool_name	src/mindroom/ai.py:294
team_response_stream.<locals>._start_tool	nested_function	lines 2134-2159	duplicate-found	format_tool_started_event pending trace visible index	src/mindroom/ai.py:467
team_response_stream.<locals>._complete_tool	nested_function	lines 2161-2208	duplicate-found	extract_tool_completed_info pending pop complete_pending_tool_block	src/mindroom/ai.py:494
team_response_stream.<locals>._start_tool_for_member	nested_function	lines 2210-2221	related-only	member visible text wrapper start tool	none
team_response_stream.<locals>._apply_visible_text	nested_function	lines 2214-2215	none-found	member visible append closure	none
team_response_stream.<locals>._complete_tool_for_member	nested_function	lines 2223-2238	related-only	member visible text wrapper complete tool	none
team_response_stream.<locals>._get_visible_text	nested_function	lines 2227-2228	none-found	member visible getter closure	none
team_response_stream.<locals>._set_visible_text	nested_function	lines 2230-2231	none-found	member visible setter closure	none
```

## Findings

### 1. Team execution preparation is implemented twice

`src/mindroom/teams.py:1444` prepares a materialized team by calling `prepare_bound_team_run_context`, marking timing, building prepared-history metadata, merging Matrix metadata, adding tool schema and model params, and returning messages plus run metadata.
`src/mindroom/api/openai_compat.py:1473` repeats the same core preparation and metadata construction for OpenAI-compatible team completions.

The OpenAI path has fewer knobs: it does not pass compaction lifecycle, thread-history render limits, pipeline timing, or system enrichment items.
Those differences are parameter differences, not a different behavior family.

### 2. Team streaming repeats agent streaming tool tracking

`src/mindroom/teams.py:2109`, `src/mindroom/teams.py:2134`, and `src/mindroom/teams.py:2161` implement pending-tool matching by call id or tool name, start-event trace creation, completion-event trace extraction, visible marker replacement, and warning logs for missing starts.
The same mechanics exist for agent streaming in `src/mindroom/ai.py:294`, `src/mindroom/ai.py:467`, and `src/mindroom/ai.py:494`.

Team streaming has one extra dimension: the `scope_key` separates team-level tools from member-level tools.
That looks parameterizable in a small helper because agent streaming is equivalent to one implicit scope.

### 3. Team response lifecycle mirrors agent response lifecycle

`src/mindroom/teams.py:1522` and `src/mindroom/teams.py:1909` follow the same high-level lifecycle as `src/mindroom/ai.py:844` and `src/mindroom/ai.py:1366`: prepare run context, build Matrix run metadata, call Agno, retry once without inline media before visible progress, scrub queued-notice state on retry, clean queued notices in `finally`, record completed/interrupted turns, convert errors to user-friendly text, and close runtime DBs.

The team code must preserve team-specific formatting, recursive member response handling, shared team storage, and multi-member tool trace extraction.
The duplicated retry and terminal-status handling are smaller than the full response loop, so extracting only those helpers would be safer than merging agent and team response orchestration.

### 4. Interrupted-response extraction has duplicated cancellation boilerplate and tool trace traversal

`src/mindroom/teams.py:1148`, `src/mindroom/teams.py:1164`, `src/mindroom/teams.py:1178`, and `src/mindroom/teams.py:1192` duplicate the agent-side ideas in `src/mindroom/ai.py:275`, `src/mindroom/ai.py:289`, `src/mindroom/ai.py:303`, and `src/mindroom/ai.py:308`.

The team variant adds recursion through nested `TeamRunOutput.member_responses`.
A shared helper could accept `RunOutput | TeamRunOutput` and recurse only when given a team output.

### 5. MatrixID order-preserving dedupe appears in multiple places

`src/mindroom/teams.py:753` filters and deduplicates Matrix IDs by `full_id` while preserving order.
`src/mindroom/thread_utils.py:262` performs the same order-preserving dedupe when collecting mentioned agents from thread history.

This is real but low-impact duplication because the call sites are small and local.

## Proposed Generalization

1. Extract a public team preparation helper that covers `teams.py` and OpenAI-compatible team preparation, probably in `src/mindroom/execution_preparation.py` or a small new `src/mindroom/team_execution.py` module.
2. Extract scoped streaming tool tracking into a small dataclass/helper in `src/mindroom/tool_system/events.py` or a focused stream helper module; model agent streaming as a single default scope and team streaming as multiple scopes.
3. Extract run-output terminal helpers for `status == error/cancelled`, cancellation boilerplate detection, interrupted text, and recursive tool trace extraction into a shared module used by `ai.py` and `teams.py`.
4. Do not merge full agent and team response functions; keep orchestration separate and share only retry/terminal-state helpers.
5. Leave MatrixID dedupe alone unless another nearby refactor already touches both `teams.py` and `thread_utils.py`.

## Risk/tests

The highest-risk area is streaming tool tracking because visible text, canonical replay text, pending trace metadata, and Matrix tool trace payloads must stay aligned.
Tests should cover tool start/completion ordering, missing start events, duplicate tool names without call IDs, hidden tool calls, and member-vs-team tool scopes.

For team preparation, tests should compare `run_metadata`, prepared prompt text, unseen event IDs, tool schema, and model params between the existing team path and OpenAI-compatible path.

For response lifecycle helpers, tests should cover inline-media retry before and after visible progress, errored `RunOutput`, cancelled `RunOutput`, `TeamRunCancelledEvent`, and queued-notice cleanup for both agent and team session types.

Assumption: this audit intentionally did not edit production code, so the proposed generalizations are recommendations only.
