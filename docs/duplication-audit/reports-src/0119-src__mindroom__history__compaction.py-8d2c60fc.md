Summary: No meaningful duplication found.

The closest candidates are narrow overlaps around compaction threshold resolution in `src/mindroom/history/policy.py`, session summary presence checks in `src/mindroom/history/runtime.py`, and generic token serialization in `src/mindroom/token_budget.py`.
These are either already shared through `compaction.py` exports or are small callers with different return shapes, so I do not recommend a refactor from this audit.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_CompactionProviderTimeoutError	class	lines 106-111	none-found	provider TimeoutError wait_for wrapper detached compaction	request_task_cancel src/mindroom/cancellation.py:1
_CompactionProviderTimeoutError.__init__	method	lines 109-111	none-found	TimeoutError original exception wrapper	none
_consume_detached_compaction_request_result	function	lines 114-125	none-found	add_done_callback result unhandled detached task	logger warning detached compaction src/mindroom/history/compaction.py:151
_warn_if_detached_compaction_request_still_running	function	lines 128-140	none-found	call_later detached task still running grace timeout	none
_detach_cancelled_compaction_request	function	lines 143-162	none-found	request_task_cancel add_done_callback call_later timeout	src/mindroom/cancellation.py:1
_ExcerptBlock	class	lines 166-175	none-found	excerpt block open_tag close_tag render max_chars	none
_ExcerptBlock.render	method	lines 171-175	none-found	truncate excerpt escape xml join tag content	none
ResolvedCompactionRuntime	class	lines 179-183	none-found	resolved compaction runtime model_name context_window	src/mindroom/history/types.py:63
_CompactionRewriteResult	class	lines 187-191	none-found	compaction rewrite result summary run ids messages	none
_GeneratedSummaryChunk	class	lines 195-197	none-found	generated summary chunk included_runs	none
_persist_cleared_force_state_if_needed	function	lines 200-224	related-only	read_scope_state write_scope_state upsert latest session	src/mindroom/history/runtime.py:469, src/mindroom/history/storage.py:84
_emit_compaction_hook	async_function	lines 227-265	related-only	CompactionHookContext emit hook runtime context	src/mindroom/hooks/enrichment.py:10, src/mindroom/tool_system/runtime_context.py:1
_should_collect_compaction_hook_messages	function	lines 268-274	none-found	has_hooks compaction before after runtime context	none
compact_scope_history	async_function	lines 278-425	none-found	compact scope history rewrite session outcome hooks	src/mindroom/history/runtime.py:480, src/mindroom/history/runtime.py:610
compact_scope_history.<locals>.emit_before_persist	nested_async_function	lines 326-335	none-found	before persist compaction hook messages callback	none
_rewrite_working_session_for_compaction	async_function	lines 429-597	none-found	rewrite working session compaction loop chunk persist	none
_emit_lifecycle_progress_after_persist	async_function	lines 600-647	related-only	CompactionLifecycleProgress progress_callback after_tokens remaining	src/mindroom/history/runtime.py:489, src/mindroom/history/runtime.py:640
_persist_compaction_progress	function	lines 650-677	related-only	deep_merge_metadata metadata_with_seen_event_ids remove runs upsert	src/mindroom/history/storage.py:84, src/mindroom/metadata_merge.py:1
_sync_remaining_runs_from_working	function	lines 680-692	none-found	sync remaining runs from working by run_id deepcopy	none
estimate_static_tokens	function	lines 695-705	related-only	agent role instructions full_prompt chars tool tokens	src/mindroom/history/compaction.py:1747, src/mindroom/history/runtime.py:1002
estimate_agent_static_tokens	function	lines 708-724	related-only	estimate agent static tokens system message prepared tools	src/mindroom/history/runtime.py:1002
estimate_tool_definition_tokens	function	lines 727-733	related-only	tool definition tokens prepare tools estimation	src/mindroom/history/compaction.py:1747
estimate_team_static_tokens	function	lines 736-751	related-only	team static tokens system message prepared tools	src/mindroom/history/runtime.py:1019
agent_tool_definition_payloads_for_logging	function	lines 754-761	related-only	agent tool definition payloads logging tools_schema	src/mindroom/ai.py:1021, src/mindroom/ai.py:1516
team_tool_definition_payloads_for_logging	function	lines 764-771	related-only	team tool definition payloads logging tools_schema	src/mindroom/teams.py:1511, src/mindroom/api/openai_compat.py:1501
_estimate_prepared_tool_definition_tokens	function	lines 774-782	related-only	stable_serialize tool definitions estimate text tool instructions	src/mindroom/token_budget.py:12
_prepare_tools_for_estimation	function	lines 785-804	none-found	prepare tools for estimation Toolkit Function callable dedupe	none
_prepare_tool_for_estimation	function	lines 807-816	none-found	prepare single tool Function Toolkit dict callable	none
_toolkit_functions	function	lines 819-827	none-found	Toolkit functions async_functions tools fallback	none
_prepare_function_for_estimation	function	lines 830-835	none-found	Function model_copy process_entrypoint strict	none
_prepared_tool_definition_payloads	function	lines 838-847	related-only	payloads by tool name Function dict tool schema	src/mindroom/ai_run_metadata.py:197
_prepared_tool_name	function	lines 850-856	none-found	tool name Function dict	none
_function_payload	function	lines 859-864	none-found	Function payload name description parameters	none
_is_tool_definition_dict	function	lines 867-872	none-found	TypeGuard tool definition dict name	none
_dict_tool_payload	function	lines 875-881	none-found	dict tool payload parameters default	none
_default_function_parameters	function	lines 884-885	none-found	default function parameters object properties required	none
_prepare_team_prompt_inputs_for_estimation	function	lines 888-924	related-only	Agno team determine tools prompt estimation	src/mindroom/history/compaction.py:927
_prepare_agent_prompt_inputs_for_estimation	function	lines 927-980	related-only	Agno agent determine tools prompt estimation	src/mindroom/history/compaction.py:888
resolve_effective_compaction_threshold	function	lines 983-991	related-only	threshold_tokens threshold_percent context_window	src/mindroom/history/policy.py:210
normalize_compaction_budget_tokens	function	lines 994-998	related-only	clamp reserve budget half context window	src/mindroom/history/policy.py:189, src/mindroom/execution_preparation.py:520
effective_summary_input_budget_tokens	function	lines 1001-1006	none-found	per call summary input budget context window cap	none
resolve_compaction_runtime_settings	function	lines 1009-1027	related-only	compaction model active model context window	src/mindroom/history/policy.py:19
_generate_compaction_summary_with_retry	async_function	lines 1030-1114	none-found	compaction summary retry smaller chunk provider failure	none
_should_retry_smaller_summary_chunk	function	lines 1117-1137	none-found	retry fragments context length too many tokens timeout	none
_generate_compaction_summary	async_function	lines 1141-1197	none-found	model aresponse summary timeout detach cancellation	none
_generate_compaction_summary.<locals>._request_summary	nested_async_function	lines 1151-1160	none-found	model aresponse compaction summary prompt provider TimeoutError	none
_normalize_compaction_summary_text	function	lines 1200-1208	none-found	strip fenced markdown summary text	none
_build_summary_input	function	lines 1212-1251	none-found	previous_summary new_conversation compacted runs budget	none
_build_oversized_summary_input	function	lines 1254-1272	none-found	oversized run excerpt remaining budget	none
_serialize_oversized_run_excerpt	function	lines 1275-1300	none-found	serialize oversized run excerpt shrink char budget	none
_serialize_run_excerpt	function	lines 1303-1325	none-found	run excerpt note blocks content budget	none
_default_compaction_history_settings	function	lines 1328-1332	related-only	ResolvedHistorySettings policy all max_tool_calls none	src/mindroom/history/runtime.py:1303
_compaction_replay_messages	function	lines 1335-1344	related-only	deepcopy run messages skip_roles filter_tool_calls strip stale	src/mindroom/history/compaction.py:1584
_excerpt_blocks	function	lines 1347-1358	none-found	excerpt blocks metadata messages render content	none
_metadata_for_excerpt	function	lines 1361-1363	none-found	omit model_params tools_schema from metadata excerpt	none
_truncate_excerpt	function	lines 1366-1373	none-found	truncate text ellipsis max chars	none
_remaining_excerpt_budget	function	lines 1376-1383	none-found	remaining excerpt budget wrapper tokens	none
_compose_summary_input	function	lines 1386-1391	none-found	compose previous summary new conversation blocks	none
_estimate_serialized_run_tokens	function	lines 1394-1395	related-only	estimate serialized run tokens estimate_text_tokens	src/mindroom/token_budget.py:12
_messages_for_runs	function	lines 1398-1405	related-only	flatten replay messages for runs	src/mindroom/history/compaction.py:1335
_serialize_run	function	lines 1408-1415	none-found	serialize run metadata messages xml tags	none
_serialize_message	function	lines 1418-1427	none-found	serialize message content tool_calls media xml	none
_run_open_tag	function	lines 1430-1436	none-found	run open tag escaped attrs index run_id status	none
_message_open_tag	function	lines 1439-1445	none-found	message open tag escaped role name tool_call_id	none
_message_media_entries	function	lines 1448-1458	none-found	message media entries images audio files outputs	none
_serialize_media_payload	function	lines 1461-1464	related-only	stable serialize media payload snapshot	src/mindroom/token_budget.py:40
_media_payload_snapshot	function	lines 1467-1474	none-found	BaseModel model_dump exclude_none strip content recursive sequence	none
_render_message_content	function	lines 1477-1486	related-only	compressed_content content list stable_serialize	src/mindroom/execution_preparation.py:423, src/mindroom/api/openai_compat.py:643
_unescape_xml_content	function	lines 1489-1490	related-only	unescape gt lt amp	src/mindroom/matrix/message_builder.py:5
_escape_xml_content	function	lines 1493-1494	related-only	html escape after unescape quote false	src/mindroom/matrix/message_builder.py:5, src/mindroom/hooks/enrichment.py:19
estimate_prompt_visible_history_tokens	function	lines 1497-1510	related-only	session summary tokens history messages tokens	src/mindroom/history/runtime.py:1635
estimate_session_summary_tokens	function	lines 1513-1528	related-only	summary replay wrapper token estimate	src/mindroom/history/runtime.py:1635
estimate_history_messages_tokens	function	lines 1531-1535	related-only	sum estimated message chars div 4	src/mindroom/token_budget.py:12
_strip_stale_anthropic_replay_fields	function	lines 1538-1558	none-found	strip Anthropic signature reasoning before last user	none
_select_runs_to_compact	function	lines 1561-1581	related-only	force compact budget current tokens select visible runs	src/mindroom/history/policy.py:85
_history_messages_for_session	function	lines 1584-1599	related-only	session history messages deepcopy filter tool calls strip stale	src/mindroom/history/compaction.py:1335
_session_history_messages	function	lines 1602-1621	related-only	agent team session history by scope kind	src/mindroom/history/compaction.py:1624, src/mindroom/history/compaction.py:1639
_agent_session_history_messages	function	lines 1624-1636	related-only	AgentSession get_messages mode runs messages all	src/mindroom/history/compaction.py:1639
_team_session_history_messages	function	lines 1639-1651	related-only	TeamSession get_messages mode runs messages all	src/mindroom/history/compaction.py:1624
_history_skip_roles	function	lines 1654-1660	related-only	system_message_role skip_history_system_role standard roles	src/mindroom/history/runtime.py:1303
completed_top_level_runs	function	lines 1663-1670	related-only	completed top level runs skip statuses parent_run_id	src/mindroom/history/runtime.py:1624
runs_for_scope	function	lines 1673-1680	related-only	filter runs by scope agent team	src/mindroom/history/runtime.py:1624
_current_summary_text	function	lines 1683-1686	related-only	session summary strip or None	src/mindroom/history/runtime.py:1629
_has_stable_run_id	function	lines 1689-1690	none-found	run_id str nonempty	none
_estimated_message_chars	function	lines 1693-1696	related-only	message content tool_calls media char estimate	src/mindroom/token_budget.py:12
_remove_runs_by_id	function	lines 1699-1726	related-only	remove runs descendants by run_id parent_run_id	src/mindroom/agents.py:718, src/mindroom/turn_store.py:421
_estimate_message_media_chars	function	lines 1729-1736	related-only	media entries snapshot stable serialize char count	src/mindroom/history/compaction.py:1461
_model_identifier	function	lines 1739-1740	related-only	model id or class name logging	src/mindroom/agents.py:1137, src/mindroom/routing.py:89
_iso_utc_now	function	lines 1743-1744	related-only	UTC isoformat Z timestamp	src/mindroom/tool_system/tool_calls.py:366, src/mindroom/oauth/state.py:62
compute_prompt_token_breakdown	function	lines 1747-1776	related-only	role instructions tool_definition current prompt token breakdown	src/mindroom/history/compaction.py:695, src/mindroom/ai.py:205
```

Findings:

1. Narrow threshold-resolution overlap.
`resolve_effective_compaction_threshold()` in `src/mindroom/history/compaction.py:983` and `_resolve_replay_threshold_tokens()` in `src/mindroom/history/policy.py:210` both check explicit `threshold_tokens` before percentage/default threshold logic.
The policy helper already delegates to `resolve_effective_compaction_threshold()` for the remaining branches, so the only duplicated behavior is the explicit-token fast path.
Preserve the policy helper's narrower name and call-site semantics if touched.

2. Session summary presence and token access are related but not worth extracting.
`_current_summary_text()` in `src/mindroom/history/compaction.py:1683`, `_session_has_summary_replay()` in `src/mindroom/history/runtime.py:1629`, and `_session_summary_replay_tokens()` in `src/mindroom/history/runtime.py:1635` all inspect `session.summary.summary.strip()`.
They return different shapes: optional normalized text, boolean presence, and token count.
A shared helper would save only a couple of lines and would add cross-module coupling.

3. History message preparation has intentionally parallel paths.
`_compaction_replay_messages()` in `src/mindroom/history/compaction.py:1335` and `_history_messages_for_session()` in `src/mindroom/history/compaction.py:1584` both deepcopy messages, apply `filter_tool_calls()`, and strip stale Anthropic replay fields.
This duplication is internal to the primary module, not elsewhere under `./src`, and the two functions start from different sources: one run's messages versus session-derived messages.

Proposed generalization: No refactor recommended.

The threshold fast path could be removed from `src/mindroom/history/policy.py:210` by always calling `resolve_effective_compaction_threshold()`, but that is a cosmetic simplification and not enough to justify a production edit under this report-only task.

Risk/tests:

If the threshold helper is simplified later, cover explicit `threshold_tokens`, `threshold_percent`, and default 80 percent cases in history policy tests.
If summary access is ever centralized, cover blank summaries, missing summaries, and nonblank summaries because the current callers intentionally differ on `None`, `False`, and token count behavior.
