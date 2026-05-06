## Summary

Top duplication candidates for `src/mindroom/execution_preparation.py` are conservative and mostly caller-adapter duplication rather than copied implementation.
`PreparedExecutionContext` overlaps with prepared-run wrappers in `ai.py` and `teams.py`, and Matrix thread-history rendering overlaps with memory/thread-summary renderers that intentionally target different downstream consumers.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_PartialReplyKind	class	lines 84-88	related-only	stream status partial reply interrupted in_progress	src/mindroom/streaming.py:155; src/mindroom/response_runner.py:531
PreparedExecutionContext	class	lines 92-129	duplicate-found	PreparedExecutionContext prepared run messages unseen_event_ids prepared_history	src/mindroom/ai.py:141; src/mindroom/teams.py:172
PreparedExecutionContext.final_prompt	method	lines 106-108	duplicate-found	prompt_text prepared_prompt render_prepared_messages_text	src/mindroom/ai.py:150; src/mindroom/teams.py:180
PreparedExecutionContext.context_messages	method	lines 111-113	duplicate-found	context_messages messages[:-1]	src/mindroom/teams.py:185
PreparedExecutionContext.prepared_history	method	lines 116-129	related-only	PreparedHistoryState compaction_outcomes replay_plan replays_persisted_history	src/mindroom/ai.py:819; src/mindroom/ai_run_metadata.py:196
ThreadHistoryRenderLimits	class	lines 133-138	none-found	ThreadHistoryRenderLimits max_messages max_message_length missing_sender_label	none
_wrap_msg_body	function	lines 141-144	none-found	CDATA xml_quoteattr <msg from	src/mindroom/agent_prompts.py:9
_truncate_message_body	function	lines 147-153	related-only	truncate message body length ellipsis max_message_length	src/mindroom/streaming.py:145; src/mindroom/thread_summary.py:314
_collect_history_messages	function	lines 156-177	related-only	thread_history sender body max_messages missing_sender_label	src/mindroom/thread_summary.py:296; src/mindroom/memory/_prompting.py:36
_build_plain_prompt_with_history	function	lines 180-190	related-only	Previous conversation Current message sender body prompt_intro	src/mindroom/thread_summary.py:296
_build_matrix_prompt_with_history	function	lines 193-206	none-found	conversation msg from CDATA current_sender prompt_intro	none
build_prompt_with_thread_history	function	lines 209-233	related-only	build prompt thread_history sender body current message	src/mindroom/thread_summary.py:296; src/mindroom/memory/_prompting.py:52
build_matrix_prompt_with_thread_history	function	lines 236-264	none-found	build matrix prompt thread_history CDATA current_sender	none
_classify_partial_reply	function	lines 267-290	related-only	STREAM_STATUS_CANCELLED STREAM_STATUS_STREAMING is_interrupted_partial_reply	src/mindroom/streaming.py:155; src/mindroom/response_runner.py:531
_clean_partial_reply_body	function	lines 293-295	related-only	clean_partial_reply_text strip partial reply	src/mindroom/streaming.py:171; src/mindroom/response_runner.py:531
_message_speaker_label	function	lines 298-303	related-only	ORIGINAL_SENDER_KEY message sender relayed	src/mindroom/response_runner.py:229; src/mindroom/memory/_prompting.py:42
_is_relayed_user_message	function	lines 306-309	related-only	ORIGINAL_SENDER_KEY isinstance str bool	src/mindroom/response_runner.py:229
_build_unseen_messages_header	function	lines 312-320	none-found	Messages since your last response partial reply header	none
_context_message_from_visible_message	function	lines 323-341	related-only	ResolvedVisibleMessage Message role assistant user sender body	src/mindroom/memory/_prompting.py:36
_context_messages_from_visible_messages	function	lines 344-362	related-only	thread_history to Message role assistant user max_message_length	src/mindroom/memory/_prompting.py:36
_messages_with_capped_context	function	lines 365-395	none-found	static_token_budget selected_context reversed estimate_static_tokens	none
_messages_with_current_prompt	function	lines 398-418	related-only	current prompt append Message role user copy_run_input	src/mindroom/memory/_prompting.py:48
render_prepared_messages_text	function	lines 421-423	related-only	join message.content render prepared messages text	src/mindroom/teams.py:105
render_prepared_team_messages_text	function	lines 426-434	related-only	assistant prefix render team messages	src/mindroom/teams.py:105
_build_unseen_context_messages	function	lines 437-474	none-found	unseen context messages partial_reply_kinds unseen_event_ids	none
_build_thread_history_messages	function	lines 477-517	related-only	fallback full-thread replay context messages thread history	src/mindroom/memory/_prompting.py:36; src/mindroom/thread_summary.py:296
_fallback_static_token_budget	function	lines 520-524	related-only	normalize_compaction_budget_tokens context_window reserve_tokens	src/mindroom/history/policy.py:197; src/mindroom/history/policy.py:231; src/mindroom/history/policy.py:245
_thread_history_before_current_event	function	lines 527-539	none-found	current_event_id preceding_messages thread_history before	none
_sanitize_thread_history_for_replay	function	lines 542-556	none-found	sanitize thread history replay unseen messages sender	none
_get_unseen_event_ids_for_metadata	function	lines 559-571	none-found	unseen event ids in_progress_event_ids metadata	none
_get_unseen_messages_for_sender	function	lines 574-619	none-found	seen_event_ids current_event_id compaction notice partial reply	none
_scope_seen_event_ids	function	lines 622-626	related-only	read_scope_seen_event_ids scope_context session scope	src/mindroom/history/storage.py:251
_finalize_prepared_history	function	lines 630-642	related-only	finalize_history_preparation timed system_prompt_assembly	src/mindroom/history/runtime.py:128
_prepare_execution_context_common	async_function	lines 645-748	none-found	prepare execution context common unseen replay fallback final messages	none
prepare_agent_execution_context	async_function	lines 752-828	related-only	prepare agent execution context prepare_scope_history estimate tokens apply replay	src/mindroom/ai.py:789
prepare_agent_execution_context.<locals>._prepare_agent_scope_history	nested_async_function	lines 779-799	related-only	prepare_scope_history agent static_prompt_tokens active_model_name	src/mindroom/history/runtime.py:128
prepare_agent_execution_context.<locals>._estimate_agent_static_tokens	nested_function	lines 801-807	related-only	estimate_preparation_static_tokens agent full_prompt	src/mindroom/history/runtime.py:29
prepare_bound_team_execution_context	async_function	lines 831-902	related-only	prepare bound team execution context prepare_bound_scope_history estimate team tokens	src/mindroom/teams.py:1476
prepare_bound_team_execution_context.<locals>._prepare_team_scope_history	nested_async_function	lines 854-870	related-only	prepare_bound_scope_history agents team full_prompt active model	src/mindroom/history/runtime.py:128
prepare_bound_team_execution_context.<locals>._estimate_team_static_tokens	nested_function	lines 872-878	related-only	estimate_preparation_static_tokens_for_team full_prompt	src/mindroom/history/runtime.py:32
_scrub_bound_team_scope_context	function	lines 905-915	related-only	scrub_queued_notice_session_context team entity_name	src/mindroom/ai.py:966; src/mindroom/ai.py:1459; src/mindroom/teams.py:334
prepare_bound_team_run_context	async_function	lines 918-967	related-only	prepare bound team run context apply_replay_plan scrub queued notice	src/mindroom/ai.py:807; src/mindroom/teams.py:1476
```

## Findings

### 1. Prepared execution wrappers duplicate a subset of `PreparedExecutionContext`

`src/mindroom/execution_preparation.py:92` defines the canonical prepared execution result with `messages`, `unseen_event_ids`, replay/compaction fields, `final_prompt`, `context_messages`, and `prepared_history`.
`src/mindroom/ai.py:141` defines `_PreparedAgentRun` with `messages`, `unseen_event_ids`, `prepared_history`, and a `prompt_text` property that renders those messages.
`src/mindroom/teams.py:172` defines `_PreparedMaterializedTeamExecution` with `messages`, `unseen_event_ids`, metadata, `prepared_prompt`, and `context_messages`.

The behavior is not fully identical because the caller wrappers carry execution-specific fields: `_PreparedAgentRun` owns the materialized `Agent` and exposes a mutable deep-copied `run_input`, while `_PreparedMaterializedTeamExecution` carries already-built Matrix run metadata.
Still, the prompt-rendering and context-slicing behavior duplicates `PreparedExecutionContext` properties.

### 2. Matrix visible-history shaping is repeated for different consumers

`src/mindroom/execution_preparation.py:156`, `src/mindroom/execution_preparation.py:323`, and `src/mindroom/execution_preparation.py:344` filter visible Matrix messages, derive sender labels, skip empty bodies, and render them into either prompt text or Agno `Message` objects.
`src/mindroom/memory/_prompting.py:36` performs a simpler conversion from the same `ResolvedVisibleMessage` inputs into role/content dictionaries for memory save calls.
`src/mindroom/thread_summary.py:296` performs another simple `sender: body` renderer for thread summary generation.

These paths share the broad behavior of iterating visible thread history and preserving non-empty message bodies with speaker information.
They are not safe to collapse wholesale because the role mapping is different: execution preparation treats the responding bot as `assistant`, memory maps `user_id` to `user`, and thread summaries intentionally exclude prior summary notices and sample long threads.

## Proposed Generalization

For finding 1, consider removing `prompt_text`/`prepared_prompt`/`context_messages` duplicate properties from caller wrappers if call sites can read them from `PreparedExecutionContext` before adding metadata or agent/team handles.
A small generic wrapper such as `PreparedExecutionEnvelope[T]` would be possible, but it is probably more abstraction than this duplication warrants.

For finding 2, no refactor is recommended now.
The shared code would need enough parameters for role mapping, summary-notice filtering, truncation versus exclusion, speaker fallback, Matrix XML wrapping, and output type that it would likely obscure the consumer-specific rules.

## Risk/tests

The result-wrapper cleanup risk is low but would touch agent and team response setup.
Tests should cover non-stream and streaming agent responses, materialized team responses, `unseen_event_ids` metadata, and prompt text used for logs/token estimates.

The visible-history rendering paths are high-risk to generalize because small role-label differences affect memory extraction, thread summaries, and LLM prompt semantics.
If generalized later, targeted tests should pin empty-body skipping, relayed user labels via `ORIGINAL_SENDER_KEY`, assistant-role detection for bot-authored messages, summary-notice exclusion, and max-message/max-length behavior.
