## Summary

Top duplication candidate: `_note_attempt_run_id` in `src/mindroom/ai_runtime.py` duplicates the same helper in `src/mindroom/ai.py`.
The queued-message notice functions are closely related to `response_lifecycle.py`, `teams.py`, and `execution_preparation.py`, but the behavior is split by responsibility rather than duplicated.
The run-input/media helpers are reused from `ai.py`, `teams.py`, `thread_summary.py`, and `topic_generator.py`; I found related call-site patterns but no second implementation of the same normalization/media mutation behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_normalize_run_input	function	lines 58-62	related-only	ModelRunInput Message model_copy deep copy string prompt	src/mindroom/ai.py:156, src/mindroom/thread_summary.py:347, src/mindroom/topic_generator.py:101
copy_run_input	function	lines 65-67	related-only	copy_run_input run_input deep copied mutable messages	src/mindroom/ai.py:156, src/mindroom/ai.py:639, src/mindroom/ai.py:1029, src/mindroom/ai.py:1523
attach_media_to_run_input	function	lines 70-81	related-only	attach_media_to_run_input audio images files videos MediaInputs	src/mindroom/ai.py:1559, src/mindroom/teams.py:1655, src/mindroom/teams.py:1883, src/mindroom/teams.py:2267
append_inline_media_fallback_to_run_input	function	lines 84-94	related-only	append_inline_media_fallback_prompt retry without inline media audio images files videos	src/mindroom/media_fallback.py:34, src/mindroom/ai.py:1074, src/mindroom/ai.py:1093, src/mindroom/ai.py:1597, src/mindroom/ai.py:1606
_SupportsQueuedMessageState	class	lines 97-98	related-only	has_pending_human_messages queued signal protocol pending human messages	src/mindroom/response_lifecycle.py:37, src/mindroom/response_lifecycle.py:66
_SupportsQueuedMessageState.has_pending_human_messages	method	lines 98-98	related-only	has_pending_human_messages pending_human_messages > 0	src/mindroom/response_lifecycle.py:66
_QueuedMessageNoticeContext	class	lines 102-103	related-only	QueuedMessageNoticeContext ContextVar queued_message_notice_context state	src/mindroom/response_lifecycle.py:241
queued_message_signal_context	function	lines 113-121	related-only	queued_message_signal_context ContextVar set reset response lifecycle	src/mindroom/response_lifecycle.py:13, src/mindroom/response_lifecycle.py:241
_has_queued_notice_marker	function	lines 124-126	none-found	mindroom_queued_message_notice provider_data marker	none
_is_queued_notice_message	function	lines 129-131	none-found	hidden queued-message notice is queued notice message	none
_strip_queued_notice_messages	function	lines 134-142	none-found	strip queued notice messages filtered_messages provider_data marker	none
_append_queued_notice_if_needed	function	lines 145-162	none-found	append queued notice stop_after_tool_call function_call_results has_pending_human_messages	none
_cleanup_queued_notice_from_run_output	function	lines 165-174	none-found	cleanup queued notice run output member_responses TeamRunOutput RunOutput messages	none
_load_session_for_cleanup	function	lines 177-190	none-found	AgentSession.from_dict TeamSession.from_dict get_session SessionType cleanup	none
_strip_queued_notice_from_session	function	lines 193-198	none-found	strip queued notice from session runs RunOutput TeamRunOutput	none
_strip_queued_notice_from_session_storage	function	lines 201-220	none-found	get_session upsert_session strip queued notice storage SessionType	none
cleanup_queued_notice_state	function	lines 223-247	related-only	cleanup_queued_notice_state strip returned persisted run state session history	src/mindroom/ai.py:1105, src/mindroom/ai.py:1721, src/mindroom/teams.py:311, src/mindroom/teams.py:319
scrub_queued_notice_session_context	function	lines 250-267	related-only	scrub queued notice session context loaded session before replay	src/mindroom/ai.py:966, src/mindroom/ai.py:1459, src/mindroom/teams.py:328, src/mindroom/execution_preparation.py:905
install_queued_message_notice_hook	function	lines 270-319	none-found	install queued message notice hook format_function_call_results _handle_function_call_media	none
install_queued_message_notice_hook.<locals>._format_function_call_results_with_notice	nested_function	lines 281-296	none-found	format_function_call_results with notice append queued notice function_call_results	none
install_queued_message_notice_hook.<locals>._handle_function_call_media_with_notice	nested_function	lines 298-311	none-found	handle_function_call_media with notice append queued notice function_call_results	none
_note_attempt_run_id	function	lines 322-325	duplicate-found	note_attempt_run_id run_id_callback current run_id before attempt	src/mindroom/ai.py:189, src/mindroom/ai.py:1544, src/mindroom/response_runner.py:1022, src/mindroom/response_runner.py:1548, src/mindroom/response_runner.py:1639, src/mindroom/teams.py:1652, src/mindroom/teams.py:2253
cached_agent_run	async_function	lines 328-349	related-only	cached_agent_run agent.arun attach media session_id user_id run_id metadata	src/mindroom/ai.py:644, src/mindroom/thread_summary.py:347, src/mindroom/topic_generator.py:101
```

## Findings

### Duplicate run-id callback helper

- `src/mindroom/ai_runtime.py:322` defines `_note_attempt_run_id(run_id_callback, run_id)` and calls the callback only when both the callback and run id are present.
- `src/mindroom/ai.py:189` defines the same helper with the same signature, docstring intent, and two-condition behavior.
- `src/mindroom/ai.py:1544` uses the local copy in the streaming path, while `src/mindroom/ai_runtime.py:341` uses the ai-runtime copy from `cached_agent_run`.

Why this is duplicated: both helpers publish an Agno run id immediately before a real run attempt.
There are no behavior differences to preserve between the two helper implementations.

Related but not counted as duplicate implementations:

- `src/mindroom/teams.py:1652` and `src/mindroom/teams.py:2253` perform the same inline two-condition callback check in team run paths.
- `src/mindroom/response_runner.py:1022`, `src/mindroom/response_runner.py:1548`, and `src/mindroom/response_runner.py:1639` define richer local callbacks that update stop manager and turn-recorder state, so they are related call sites rather than duplicates of the simple helper.

## Proposed Generalization

Move the simple run-id notification helper to one shared location and use it from `ai.py`, `ai_runtime.py`, and team call sites that only need the two-condition callback check.
The smallest location is `src/mindroom/ai_runtime.py` because that module already owns run-attempt helpers and is already imported by `ai.py` and `teams.py`.

No refactor is recommended for queued-notice cleanup or hook installation.
Those functions are already centralized in `ai_runtime.py`, and the other modules call them rather than reimplementing them.

No refactor is recommended for run-input normalization/media attachment.
The repeated code found in `ai.py`, `teams.py`, `thread_summary.py`, and `topic_generator.py` is call-site orchestration around existing shared helpers, not duplicated helper logic.

## Risk/tests

If the run-id helper is deduplicated later, tests should cover:

- Non-streaming agent runs still publish the initial `run_id` before `agent.arun`.
- Streaming agent and team retries still publish retry run ids only when a callback and run id are present.
- Response runner callbacks that also update stop-manager and turn-recorder state must not be collapsed into the simple helper unless their extra side effects remain explicit.

No production code was edited for this audit.
