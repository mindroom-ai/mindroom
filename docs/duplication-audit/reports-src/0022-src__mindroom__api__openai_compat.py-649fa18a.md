# Summary

Top duplication candidate: OpenAI team preparation in `src/mindroom/api/openai_compat.py` repeats the same prepare-bound-team-run-context plus run-metadata construction flow used by Matrix team execution in `src/mindroom/teams.py`.
Tool streaming state and event formatting are related to Matrix streaming paths in `src/mindroom/ai.py`, `src/mindroom/teams.py`, and `src/mindroom/streaming_delivery.py`, but the OpenAI adapter emits different inline `<tool>` tags and SSE chunks, so this is related behavior rather than a clean duplicate.
Bearer-token parsing in `_authenticate_request` overlaps with `src/mindroom/api/auth.py`, but the credential source and OpenAI-shaped errors are endpoint-specific.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_ToolStreamState	class	lines 118-122	related-only	tool_ids_by_call_id pending_tools tool stream state	src/mindroom/ai.py:469; src/mindroom/ai.py:496; src/mindroom/teams.py:2142; src/mindroom/streaming_delivery.py:230
_run_openai_response_backgrounds	async_function	lines 125-152	none-found	BackgroundTask always_background completion_predicate finalizer-safe response	none
_OpenAIJSONResponse	class	lines 155-176	none-found	JSONResponse background always_background completion-scoped finalizer	none
_OpenAIJSONResponse.__call__	async_method	lines 160-176	none-found	JSONResponse __call__ background completion_background always_background	none
_OpenAIStreamingResponse	class	lines 179-202	none-found	StreamingResponse completion_predicate always_background	none
_OpenAIStreamingResponse.__call__	async_method	lines 185-202	none-found	StreamingResponse __call__ completion_predicate background finalizer	none
_openai_completion_lock	function	lines 205-223	none-found	session lock completion lock per agent session	none
_release_openai_completion_lock	function	lines 226-228	none-found	release completion_lock locked release	none
_attach_openai_completion_lock_release	function	lines 231-240	none-found	attach lock release response finalizer BackgroundTask	none
_PreparedOpenAITeamPrompt	class	lines 244-248	not-a-behavior-symbol	dataclass prepared team prompt	none
_PreparedOpenAIMaterializedTeamExecution	class	lines 252-256	not-a-behavior-symbol	dataclass prepared materialized team execution	none
_openai_team_request_log_context	function	lines 259-280	related-only	build_llm_request_log_context team request log context	src/mindroom/teams.py:1560; src/mindroom/teams.py:2290
_load_config	function	lines 283-296	related-only	read_committed_runtime_config bind request snapshot	src/mindroom/api/auth.py:334; src/mindroom/api/main.py:400
_openai_compatible_agent_names	function	lines 299-306	related-only	router exclusion agent compatibility worker scope	src/mindroom/team_exact_members.py:49; src/mindroom/response_runner.py:919
_openai_incompatible_agents	function	lines 309-316	related-only	unsupported worker scope delegation closure	src/mindroom/config/main.py:626; src/mindroom/agent_policy.py:201
_openai_incompatible_agent_closure	function	lines 319-334	related-only	get_agent_delegation_closure get_agent_execution_scope worker_scope	src/mindroom/config/main.py:1446; src/mindroom/agent_policy.py:201
_unsupported_worker_scope_error	function	lines 337-375	related-only	unsupported worker_scope private.per shared agents error	src/mindroom/config/main.py:626; src/mindroom/tool_system/worker_routing.py:306; src/mindroom/api/credentials.py:608
_validate_team_model_request	function	lines 378-389	related-only	team model validation teams not found incompatible agents	src/mindroom/turn_policy.py:361; src/mindroom/bot.py:1876
_validate_agent_model_request	function	lines 392-403	related-only	agent model validation router reserved model not found	src/mindroom/entity_resolution.py:128; src/mindroom/ai_run_metadata.py:42
_ChatMessage	class	lines 411-415	not-a-behavior-symbol	OpenAI chat message pydantic role content	none
_ChatCompletionRequest	class	lines 418-442	not-a-behavior-symbol	OpenAI chat completion request pydantic	none
_ChatCompletionChoice	class	lines 448-453	not-a-behavior-symbol	OpenAI chat completion choice pydantic	none
_UsageInfo	class	lines 456-461	not-a-behavior-symbol	OpenAI usage pydantic prompt_tokens	none
_ChatCompletionResponse	class	lines 464-473	not-a-behavior-symbol	OpenAI chat completion response pydantic	none
_ChatCompletionChunkChoice	class	lines 479-484	not-a-behavior-symbol	OpenAI streaming chunk choice pydantic	none
_ChatCompletionChunk	class	lines 487-495	not-a-behavior-symbol	OpenAI streaming chunk pydantic	none
_ModelObject	class	lines 501-509	not-a-behavior-symbol	OpenAI model object pydantic	none
_ModelListResponse	class	lines 512-516	not-a-behavior-symbol	OpenAI model list pydantic	none
_OpenAIError	class	lines 522-528	not-a-behavior-symbol	OpenAI error pydantic	none
_OpenAIErrorResponse	class	lines 531-534	not-a-behavior-symbol	OpenAI error response pydantic	none
_error_response	function	lines 542-553	none-found	OpenAI-style error response invalid_request_error	none
_authenticate_request	function	lines 556-589	related-only	Bearer Authorization token API keys allow unauthenticated	src/mindroom/api/auth.py:177; src/mindroom/api/auth.py:637
_is_error_response	function	lines 592-617	related-only	user friendly error raw provider error emoji RunErrorEvent	src/mindroom/error_handling.py:29; src/mindroom/ai.py:956; src/mindroom/teams.py:1711
_looks_like_raw_provider_error	function	lines 633-639	related-only	raw provider error Error code JSON error prefixes	src/mindroom/media_fallback.py:18; src/mindroom/error_handling.py:29
_extract_content_text	function	lines 642-652	none-found	OpenAI multimodal content list text parts	none
_find_last_user_message	function	lines 655-667	related-only	last user message split prompt thread_history	src/mindroom/execution_preparation.py:527; src/mindroom/response_runner.py:220
_convert_messages	function	lines 670-717	related-only	OpenAI messages to ResolvedVisibleMessage synthetic thread_history	src/mindroom/execution_preparation.py:209; src/mindroom/memory/_prompting.py:37
_derive_session_id	function	lines 720-747	none-found	x-session-id x-librechat-conversation-id sha256 auth namespace	none
_validate_chat_request	function	lines 750-766	related-only	validate request model auto team agent messages	src/mindroom/entity_resolution.py:128; src/mindroom/turn_policy.py:361
_parse_chat_request	function	lines 769-793	related-only	json loads pydantic ValidationError request body validation	src/mindroom/api/auth.py:558; src/mindroom/api/main.py:479
_resolve_auto_route	async_function	lines 796-821	related-only	suggest_agent auto-routing available agents fallback	src/mindroom/routing.py:1; src/mindroom/turn_policy.py:403
_request_knowledge_refresh_scheduler	function	lines 824-826	related-only	app_state knowledge_refresh_scheduler request	src/mindroom/api/main.py:400; src/mindroom/orchestrator.py:287
_log_missing_knowledge_bases	function	lines 829-835	related-only	knowledge bases not available warning callback	src/mindroom/ai.py:712; src/mindroom/teams.py:1560
list_models	async_function	lines 844-903	none-found	/v1/models OpenAI model list agents teams auto	none
chat_completions	async_function	lines 907-1044	related-only	agent/team completion dispatch knowledge resolution execution identity lock	src/mindroom/response_runner.py:719; src/mindroom/teams.py:1528
_non_stream_completion	async_function	lines 1052-1099	related-only	ai_response non-streaming OpenAI chat completion	src/mindroom/ai.py:850; src/mindroom/response_runner.py:1813
_chunk_json	function	lines 1107-1123	none-found	chat.completion.chunk model_dump_json SSE chunk	none
_extract_tool_call_id	function	lines 1126-1132	related-only	tool_call_id extraction tool_execution_call_id	src/mindroom/history/interrupted_replay.py:99; src/mindroom/teams.py:2142
_allocate_next_tool_id	function	lines 1135-1138	related-only	next tool id increment tool index	src/mindroom/ai.py:469; src/mindroom/teams.py:2142; src/mindroom/streaming_delivery.py:245
_raise_unmaterializable_team	function	lines 1141-1143	related-only	team cannot be materialized ValueError reason	src/mindroom/teams.py:1087; src/mindroom/teams.py:1203
_resolve_started_tool_id	function	lines 1146-1155	related-only	match started tool call id pending tool state	src/mindroom/ai.py:469; src/mindroom/teams.py:2142; src/mindroom/streaming_delivery.py:230
_resolve_completed_tool_id	function	lines 1158-1165	related-only	match completed tool call id pending tool state	src/mindroom/ai.py:496; src/mindroom/teams.py:2175; src/mindroom/streaming_delivery.py:270
_inject_tool_metadata	function	lines 1168-1169	none-found	OpenAI inline tool tag id state	none
_escape_tool_payload_text	function	lines 1172-1173	related-only	escape XML HTML tool payload	src/mindroom/history/compaction.py:1421
_format_openai_tool_call_display	function	lines 1176-1180	related-only	tool call display name args preview	src/mindroom/tool_system/events.py:1; src/mindroom/history/interrupted_replay.py:127
_format_openai_stream_tool_message	function	lines 1183-1201	related-only	format_tool_started_event format_tool_completed_event result_preview	src/mindroom/ai.py:469; src/mindroom/streaming_delivery.py:245; src/mindroom/teams.py:2142
_format_stream_tool_event	function	lines 1204-1228	related-only	ToolCallStartedEvent ToolCallCompletedEvent stream tool text	src/mindroom/ai.py:1256; src/mindroom/teams.py:2392; src/mindroom/streaming_delivery.py:230
_extract_stream_text	function	lines 1231-1237	related-only	RunContentEvent string tool event extraction	src/mindroom/ai.py:1249; src/mindroom/streaming_delivery.py:230
_extract_agent_stream_failure	function	lines 1240-1246	related-only	RunErrorEvent error string stream failure	src/mindroom/ai.py:1300; src/mindroom/teams.py:2348
_stream_completion	async_function	lines 1249-1368	related-only	stream_agent_response SSE OpenAI chunks preflight first event	src/mindroom/ai.py:1342; src/mindroom/response_runner.py:1963
_stream_completion.<locals>.event_generator	nested_async_function	lines 1307-1361	related-only	SSE event_generator role content finish DONE aclose	src/mindroom/api/openai_compat.py:1817; src/mindroom/streaming_delivery.py:230
_build_team	function	lines 1376-1436	related-only	materialize_exact_team_members resolve_configured_team build_materialized_team_instance	src/mindroom/teams.py:1560; src/mindroom/teams.py:2008
_format_team_output	function	lines 1439-1442	related-only	format_team_response join content fallback	src/mindroom/teams.py:269; src/mindroom/teams.py:298; src/mindroom/teams.py:1154
_is_failed_team_output	function	lines 1445-1447	related-only	is_errored_run_output is_cancelled_run_output	src/mindroom/teams.py:284; src/mindroom/teams.py:290; src/mindroom/teams.py:1715
prepare_materialized_team_execution	async_function	lines 1450-1514	duplicate-found	prepare_bound_team_run_context build_matrix_run_metadata team_tool_definition_payloads_for_logging	src/mindroom/teams.py:1450; src/mindroom/teams.py:1476; src/mindroom/teams.py:1504
_prepare_openai_team_prompt	async_function	lines 1517-1554	related-only	prepare materialized team render_prepared_team_messages_text OpenAI prompt	src/mindroom/teams.py:183; src/mindroom/execution_preparation.py:426
_non_stream_team_completion	async_function	lines 1557-1677	related-only	team.arun non-stream team completion OpenAI response	src/mindroom/teams.py:1528; src/mindroom/teams.py:1560
_stream_team_completion	async_function	lines 1680-1849	related-only	team.arun stream_events OpenAI SSE preflight cleanup	src/mindroom/teams.py:1915; src/mindroom/teams.py:2290
_stream_team_completion.<locals>._cleanup	nested_async_function	lines 1700-1708	related-only	aclose stack close team runtime state db cleanup	src/mindroom/teams.py:1835; src/mindroom/mcp/manager.py:285
_stream_team_completion.<locals>.mark_stream_failed	nested_function	lines 1813-1815	not-a-behavior-symbol	flag setter stream_failed	none
_stream_team_completion.<locals>._event_generator	nested_async_function	lines 1817-1833	related-only	wrap team stream generator cleanup completion flag	src/mindroom/api/openai_compat.py:1307; src/mindroom/teams.py:2290
_extract_team_stream_failure	function	lines 1852-1863	related-only	TeamRunErrorEvent TeamRunCancelledEvent failed output	src/mindroom/teams.py:1715; src/mindroom/teams.py:2295; src/mindroom/ai.py:1300
_classify_team_event	function	lines 1866-1893	related-only	classify team content tool events skip member content	src/mindroom/teams.py:2392; src/mindroom/teams.py:2407; src/mindroom/teams.py:2413
_finalize_pending_tools	function	lines 1896-1904	related-only	pending tools interrupted finalize done tags	src/mindroom/history/interrupted_replay.py:127; src/mindroom/response_runner.py:123
_team_stream_event_generator	async_function	lines 1907-1971	related-only	team stream SSE chunks tool finalization DONE	src/mindroom/api/openai_compat.py:1307; src/mindroom/teams.py:2290; src/mindroom/streaming_delivery.py:230
_team_stream_event_generator.<locals>._chunk	nested_function	lines 1929-1930	none-found	local SSE content chunk helper	none
```

# Findings

## 1. Team run preparation and metadata construction are duplicated

`src/mindroom/api/openai_compat.py:1450` implements `prepare_materialized_team_execution()` by calling `prepare_bound_team_run_context()`, building prepared-history metadata with `build_prepared_history_metadata_content()`, constructing run metadata with `build_matrix_run_metadata()`, and returning prepared messages plus metadata.
`src/mindroom/teams.py:1450` implements `_prepare_materialized_team_execution()` with the same core sequence, including `prepare_bound_team_run_context()` at `src/mindroom/teams.py:1476` and `build_matrix_run_metadata()` at `src/mindroom/teams.py:1504`.

The behavior is functionally the same: both paths prepare a materialized team run against persisted Matrix/Agno history and attach tools schema, model params, requester/thread metadata, and prepared-history metadata before `team.arun()`.
Differences to preserve: Matrix team execution accepts compaction lifecycle, render limits, pipeline timing, and system enrichment items; the OpenAI path intentionally passes no Matrix room/thread event IDs and renders the prepared messages into a plain OpenAI prompt later.

## 2. Streaming tool tracking is related but protocol-specific

`src/mindroom/api/openai_compat.py:1146`, `src/mindroom/api/openai_compat.py:1158`, and `src/mindroom/api/openai_compat.py:1204` maintain started/completed tool IDs and render inline `<tool id="..." state="...">` text for SSE consumers.
Matrix streaming paths in `src/mindroom/ai.py:469`, `src/mindroom/ai.py:496`, `src/mindroom/streaming_delivery.py:230`, and `src/mindroom/teams.py:2142` also track pending tool calls and reconcile completions using `format_tool_started_event()` / `format_tool_completed_event()`.

The shared behavior is pending-tool reconciliation across Agno started/completed events.
It is not a direct duplication because Matrix paths update rich visible Matrix text, `ToolTraceEntry` lists, interrupted replay state, and delivery queues, while the OpenAI path must generate compact SSE content chunks and stable client-side tool IDs.

## 3. Bearer parsing overlaps with API auth, but endpoint semantics differ

`src/mindroom/api/openai_compat.py:556` validates `Authorization: Bearer ...` and compares against comma-separated `OPENAI_COMPAT_API_KEYS`.
`src/mindroom/api/auth.py:177` already extracts bearer tokens from an Authorization header for dashboard/Supabase auth.

The shared behavior is only header parsing.
The rest differs: OpenAI compatibility has its own env-based key list, unauthenticated development flag, and OpenAI-shaped JSON error envelope.

# Proposed Generalization

1. Extract the shared team preparation core into `src/mindroom/teams.py` or `src/mindroom/execution_preparation.py` as a small public helper that returns prepared messages, run metadata, and unseen event IDs.
2. Keep Matrix-only options optional on that helper: compaction lifecycle, render limits, pipeline timing, and system enrichment.
3. Update `src/mindroom/teams.py` and `src/mindroom/api/openai_compat.py` to call that helper, with OpenAI continuing to render messages through `render_prepared_team_messages_text()`.
4. Leave OpenAI SSE chunking, OpenAI error envelopes, and OpenAI inline tool-tag rendering local to `openai_compat.py`.
5. Optionally reuse only a tiny bearer-token extraction helper from `src/mindroom/api/auth.py` if it can be imported without coupling OpenAI compatibility to dashboard auth.

# Risk/tests

Main risk is changing prepared-history metadata or unseen-event handling for Matrix team runs while extracting the shared team preparation helper.
Tests should cover existing Matrix team preparation behavior plus OpenAI team non-stream and stream paths, including run metadata fields, tools schema, context-window handling, and prepared history rendering.
No refactor is recommended for streaming tool handling unless there is a separate protocol-neutral `PendingToolCallTracker`; the current output formats differ enough that a broad extraction would add risk.
