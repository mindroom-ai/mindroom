## Summary

Top duplication candidates:

1. `thread_summary._generate_summary` repeats the repository's one-off structured-AI task pattern used by `topic_generator.generate_room_topic_ai`, `routing.suggest_agent`, and team-mode selection in `teams`.
2. `send_thread_summary_event` repeats the Matrix delivery sequence of resolving latest thread event, building threaded `m.notice` content, sending, and notifying the conversation cache.
3. Most threshold, cache, and summary-count recovery helpers are thread-summary-specific and do not currently justify extraction.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ThreadSummaryWriteError	class	lines 52-53	related-only	ThreadSummaryWriteError manual tool error custom exceptions	src/mindroom/custom_tools/thread_summary.py:114; src/mindroom/custom_tools/subagents.py:316
ThreadSummaryWriteResult	class	lines 57-62	related-only	ThreadSummaryWriteResult event_id message_count summary result dataclasses	src/mindroom/custom_tools/thread_summary.py:123; src/mindroom/post_response_effects.py:31
_ThreadSummary	class	lines 65-71	duplicate-found	BaseModel Field output_schema summary structured AI response	src/mindroom/topic_generator.py:22; src/mindroom/routing.py:25; src/mindroom/teams.py:198
_SupportsTemperature	class	lines 75-78	related-only	temperature protocol VertexAIClaude model.temperature runtime override	src/mindroom/model_loading.py:119; src/mindroom/memory/config.py:133; src/mindroom/avatar_generation.py:257
_configure_summary_model_temperature	function	lines 81-103	related-only	VertexAIClaude temperature model.temperature unsupported temperature override	src/mindroom/vertex_claude_compat.py:47; src/mindroom/memory/config.py:133; src/mindroom/avatar_generation.py:257
normalize_thread_summary_text	function	lines 106-121	related-only	markdown strip plain text normalize summary whitespace join split	src/mindroom/custom_tools/subagents.py:67; src/mindroom/history/compaction.py:1200; src/mindroom/matrix/message_builder.py:456
thread_summary_cache_key	function	lines 124-126	none-found	thread_summary_cache_key room_id thread_id cache key	src/mindroom/thread_summary.py:131; src/mindroom/thread_summary.py:136; src/mindroom/thread_summary.py:178
thread_summary_lock	function	lines 129-131	none-found	thread_summary_lock per-thread lock defaultdict asyncio.Lock	src/mindroom/thread_summary.py:464; src/mindroom/thread_summary.py:513
update_last_summary_count	function	lines 134-139	none-found	last_summary_counts monotonically message_count update max	src/mindroom/thread_summary.py:494; src/mindroom/thread_summary.py:519; src/mindroom/thread_summary.py:537; src/mindroom/thread_summary.py:559
_next_threshold	function	lines 142-151	none-found	first_threshold subsequent_interval last_summarized_count threshold	src/mindroom/thread_summary.py:177; src/mindroom/post_response_effects.py:87
_is_thread_summary_message	function	lines 154-156	none-found	io.mindroom.thread_summary visible message metadata predicate	src/mindroom/thread_summary.py:161; src/mindroom/thread_summary.py:307
_count_non_summary_messages	function	lines 159-161	none-found	count non summary messages exclude summary notices	src/mindroom/thread_summary.py:168; src/mindroom/thread_summary.py:475; src/mindroom/thread_summary.py:527
thread_summary_message_count_hint	function	lines 164-168	none-found	thread_summary_message_count_hint lower-bound post-response count	src/mindroom/response_runner.py:1379; src/mindroom/response_runner.py:2403; src/mindroom/post_response_effects.py:44
next_thread_summary_threshold	function	lines 171-181	none-found	next_thread_summary_threshold first_threshold subsequent_interval defaults	src/mindroom/thread_summary.py:194; src/mindroom/thread_summary.py:521; src/mindroom/post_response_effects.py:94
should_queue_thread_summary	function	lines 184-195	none-found	should_queue_thread_summary prequeue concurrency margin message_count_hint	src/mindroom/post_response_effects.py:87; src/mindroom/post_response_effects.py:273
_load_thread_history	async_function	lines 198-210	related-only	get_thread_history caller_label list conversation_cache	src/mindroom/scheduling.py:1282; src/mindroom/matrix/conversation_cache.py:810; src/mindroom/custom_tools/matrix_conversation_operations.py:382
_recover_last_summary_count	async_function	lines 213-249	related-only	room_messages RoomMessagesResponse MessageDirection.back metadata scan	src/mindroom/matrix/client_thread_history.py:1081; src/mindroom/custom_tools/matrix_conversation_operations.py:390; src/mindroom/bot_room_lifecycle.py:156
_build_conversation_text	function	lines 296-319	related-only	thread_history sender body sampled first last omitted messages	src/mindroom/routing.py:74; src/mindroom/matrix/client_visible_messages.py:122; src/mindroom/matrix/message_content.py:213
_generate_summary	async_function	lines 322-355	duplicate-found	get_model_instance Agent output_schema cached_agent_run structured one-off AI	src/mindroom/topic_generator.py:89; src/mindroom/routing.py:85; src/mindroom/teams.py:615
_timed_generate_summary	async_function	lines 359-365	related-only	timed decorator maybe_generate_thread_summary wrapper duration logging	src/mindroom/post_response_effects.py:101
send_thread_summary_event	async_function	lines 368-439	duplicate-found	get_latest_thread_event_id_if_needed build_message_content m.notice send_message_result notify_outbound_message	src/mindroom/delivery_gateway.py:923; src/mindroom/delivery_gateway.py:1014; src/mindroom/hooks/sender.py:76; src/mindroom/matrix/client_delivery.py:399
set_manual_thread_summary	async_function	lines 442-499	related-only	manual summary validation load history send thread summary result error	src/mindroom/custom_tools/thread_summary.py:75; src/mindroom/custom_tools/subagents.py:67; src/mindroom/custom_tools/subagents.py:306
maybe_generate_thread_summary	async_function	lines 502-569	none-found	recover threshold load history generate normalize update send retry storms	src/mindroom/post_response_effects.py:135; src/mindroom/response_runner.py:1379
```

## Findings

### 1. One-off structured AI task flow is repeated

`src/mindroom/thread_summary.py:322` loads a configured model, builds an `Agent` with a small Pydantic output schema, runs a single prompt, inspects `response.content`, and returns a scalar field.
The same behavior appears in `src/mindroom/topic_generator.py:89`, `src/mindroom/routing.py:85`, and `src/mindroom/teams.py:615`.

These are functionally similar because each call site performs a short-lived internal AI classification/generation task with a structured schema and a stable session id.
Differences to preserve:

- Thread summaries use `cached_agent_run`, a content hash session id, summary-specific temperature handling, and `None` on absent content.
- Room topics use `cached_agent_run`, catch/log exceptions internally, and return string fallback for unexpected content.
- Routing and team-mode decisions validate that the returned enum/name is allowed before accepting it.

### 2. Threaded Matrix notice delivery sequence is repeated

`src/mindroom/thread_summary.py:368` normalizes/truncates a notice body, resolves `latest_thread_event_id`, builds `m.notice` content with metadata via `build_message_content`, calls `send_message_result`, and notifies `conversation_cache`.
The same delivery sequence appears in `src/mindroom/delivery_gateway.py:923` for compaction lifecycle notices, in `src/mindroom/delivery_gateway.py:1014` for notice edits, in `src/mindroom/hooks/sender.py:76` for hook messages, and in `src/mindroom/matrix/client_delivery.py:399` for threaded file messages.

These are functionally similar because each call site must preserve Matrix thread fallback semantics and keep the conversation cache coherent after outbound delivery.
Differences to preserve:

- Thread summaries intentionally fall back to the thread root if latest-event lookup fails.
- Compaction notices include formatted italic HTML and skip mentions.
- Hook messages use mention formatting and hook metadata rather than `build_message_content`.
- File delivery builds media content and requires `latest_thread_event_id` instead of resolving internally.

### 3. Markdown/plain-text normalization is related but already centralized for thread-summary consumers

`src/mindroom/thread_summary.py:106` strips common Markdown constructs and collapses whitespace for summary text.
`src/mindroom/custom_tools/subagents.py:67` already reuses this function for spawn summaries, so there is no duplicate summary normalizer in that path.
There are related plain-text cleanup helpers in `src/mindroom/history/compaction.py:1200`, `src/mindroom/streaming.py:213`, and `src/mindroom/matrix/message_builder.py:456`, but they target different semantics: compaction summary cleanup, stream comparison, and Markdown-to-Matrix HTML rendering.

No refactor is recommended for this symbol unless another feature needs the exact same Markdown-to-one-line plain text behavior.

## Proposed Generalization

1. Consider a small internal helper for structured one-off AI calls, probably near `mindroom.ai_runtime`, that accepts `agent_name`, `model_name`, `instructions`/`role`, `prompt`, `output_schema`, `session_id`, and an optional validator.
2. Keep the thread-summary temperature override outside that helper unless another call site needs per-request model mutation.
3. Consider a focused Matrix delivery helper for "send threaded notice and notify cache" that accepts body, metadata key/value, caller label, and fallback behavior.
4. Do not extract threshold/count/cache helpers; they are specific to thread-summary policy and currently clearer in place.
5. Do not broaden `normalize_thread_summary_text` until there is a second independent exact-use case.

## Risk/tests

Refactoring the structured-AI flow risks changing exception behavior, fallback return values, or session id stability.
Tests should cover unexpected output schema content and provider/model selection for thread summaries, room topics, routing, and team-mode decisions.

Refactoring threaded notice delivery risks Matrix thread fallback regressions and stale conversation-cache state.
Tests should cover `latest_thread_event_id` lookup success, lookup failure fallback for summaries, send failure returning `None`, and `notify_outbound_message` being called with the delivered content.

Assumption: this audit intentionally excludes production edits, so the report proposes but does not implement the generalizations.
