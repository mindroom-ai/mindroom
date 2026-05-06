Summary: One meaningful duplication candidate was found.
`src/mindroom/tool_system/events.py` owns the canonical visible tool marker regex and marker-line semantics, but `src/mindroom/response_runner.py` repeats the same marker-line pattern for replay cleanup.
The rest of the module is mostly canonical formatting used by other call sites, with related but intentionally different preview/truncation behavior in approval and custom-tool modules.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ToolTraceEntry	class	lines 34-41	related-only	ToolTraceEntry dataclass tool_trace completed interrupted metadata	src/mindroom/history/turn_recorder.py:25; src/mindroom/final_delivery.py:45; src/mindroom/hooks/context.py:240; src/mindroom/streaming.py:91; src/mindroom/delivery_gateway.py:95
StructuredStreamChunk	class	lines 45-49	related-only	StructuredStreamChunk stream chunk content tool_trace	src/mindroom/teams.py:142; src/mindroom/teams.py:2434; src/mindroom/streaming_delivery.py:35; src/mindroom/streaming_delivery.py:216
_to_compact_text	function	lines 52-58	related-only	json.dumps ensure_ascii False sort_keys compact preview text	src/mindroom/approval_manager.py:106; src/mindroom/approval_manager.py:115; src/mindroom/matrix/large_messages.py:420; src/mindroom/workers/runtime.py:33
_as_structured_result_dict	function	lines 61-64	related-only	isinstance dict cast structured result normalized content dict	src/mindroom/matrix/message_content.py:40; src/mindroom/custom_tools/matrix_api.py:385; src/mindroom/approval_manager.py:152
_as_structured_result_list	function	lines 67-70	none-found	structured result list isinstance list threads items	none
_parse_structured_result	function	lines 73-94	none-found	parse structured result threads body_preview thread_id json.loads	none
_truncate	function	lines 97-102	related-only	truncate text limit ellipsis tuple bool	src/mindroom/custom_tools/coding.py:816; src/mindroom/custom_tools/matrix_api.py:368; src/mindroom/history/compaction.py:1366
_truncate_result_item_field	function	lines 105-120	none-found	truncate result item field body_preview copy dict	none
_fit_structured_result_item	function	lines 123-159	none-found	fit structured result item binary search body_preview preview_payload	none
_drop_last_structured_result_item	function	lines 162-168	none-found	drop last structured result item preview_payload list_keys	none
_shrink_last_structured_result_item	function	lines 171-214	none-found	shrink last structured result item binary search body_preview	none
_format_structured_result_preview	function	lines 217-280	none-found	format structured result preview threads body_preview truncated payload	none
_format_tool_result_preview	function	lines 283-289	related-only	format tool result preview compact truncation result_preview	src/mindroom/approval_manager.py:152; src/mindroom/custom_tools/coding.py:975; src/mindroom/custom_tools/coding.py:1146
_neutralize_mentions	function	lines 292-294	related-only	neutralize mentions replace @ zero width skip mentions	src/mindroom/conversation_resolver.py:44; src/mindroom/custom_tools/matrix_message.py:210; src/mindroom/custom_tools/matrix_conversation_operations.py:85
_tool_marker_line	function	lines 297-301	duplicate-found	tool marker line wrench backtick index pending hourglass regex	src/mindroom/response_runner.py:105; src/mindroom/response_runner.py:134
is_visible_tool_marker_line	function	lines 304-306	duplicate-found	visible tool marker line regex fullmatch	src/mindroom/response_runner.py:105; src/mindroom/response_runner.py:141
_line_ending	function	lines 309-314	none-found	line ending CRLF newline preserve splitlines keepends	none
ensure_visible_tool_marker_spacing	function	lines 317-332	related-only	ensure visible tool marker spacing setext heading markdown	src/mindroom/matrix/mentions.py:384; tests/test_markdown_to_html.py:437; tests/test_tool_events.py:547
_format_tool_marker	function	lines 335-336	related-only	format tool marker blank lines marker line	src/mindroom/ai.py:262; src/mindroom/teams.py:2142; src/mindroom/streaming_delivery.py:255
_format_tool_args	function	lines 339-353	related-only	format tool args preview compact truncate args_preview	src/mindroom/approval_manager.py:152; src/mindroom/approval_events.py:27
_format_tool_started	function	lines 356-372	related-only	format tool started marker trace entry	src/mindroom/ai.py:476; src/mindroom/teams.py:2142; src/mindroom/streaming_delivery.py:255; src/mindroom/api/openai_compat.py:1191
format_tool_combined	function	lines 375-402	related-only	format tool combined completed marker trace result	src/mindroom/ai.py:262; src/mindroom/ai.py:284; tests/test_tool_output_files.py:655
complete_pending_tool_block	function	lines 405-441	related-only	complete pending tool block replace hourglass rfind	src/mindroom/ai.py:521; src/mindroom/teams.py:2189; src/mindroom/streaming_delivery.py:289
format_tool_started_event	function	lines 444-454	related-only	ToolExecution started event tool_name tool_args trace	src/mindroom/history/interrupted_replay.py:117; src/mindroom/ai.py:476; src/mindroom/teams.py:2142; src/mindroom/api/openai_compat.py:1191
format_tool_completed_event	function	lines 457-467	related-only	ToolExecution completed event tool_name tool_args result trace	src/mindroom/history/interrupted_replay.py:112; src/mindroom/ai.py:508; src/mindroom/teams.py:1168; src/mindroom/api/openai_compat.py:1189
extract_tool_completed_info	function	lines 470-480	related-only	extract tool completed info tool_name result event.content timing	src/mindroom/ai.py:304; src/mindroom/teams.py:2120
build_tool_trace_content	function	lines 483-513	related-only	build tool trace content io.mindroom.tool_trace content_truncated	src/mindroom/matrix/mentions.py:395; src/mindroom/matrix/large_messages.py:55; tests/test_stale_stream_cleanup.py:1599
render_tool_trace_for_context	function	lines 516-530	duplicate-found	render tool trace context lines args result truncated interrupted	src/mindroom/history/interrupted_replay.py:127
```

## Findings

### 1. Visible tool-marker recognition is duplicated outside the canonical event module

`src/mindroom/tool_system/events.py:27-28` defines tool-marker regexes and `src/mindroom/tool_system/events.py:297-306` formats and recognizes visible marker lines.
`src/mindroom/response_runner.py:105` repeats the same visible marker-line regex, and `src/mindroom/response_runner.py:134-160` uses it to strip marker lines before replay persistence.

The behavior is functionally the same because both modules need to identify the same Matrix-visible marker format: wrench icon, backticked tool name, one-based index, and optional pending hourglass.
If marker syntax changes in `events.py`, replay cleanup can silently diverge because `response_runner.py` has its own regex copy.

Differences to preserve: `response_runner.py:106` also recognizes `---` separators and `_strip_visible_tool_markers` preserves blank spacer handling around stripped markers.
Only the marker-line predicate is duplicated; separator stripping is local cleanup behavior.

### 2. Context rendering for completed and interrupted tool traces is near-duplicated

`src/mindroom/tool_system/events.py:516-530` renders completed/started tool-trace metadata for conversation history using `[tool:{name} {status}]`, optional args, result, and truncation lines.
`src/mindroom/history/interrupted_replay.py:127-136` renders interrupted tool traces with the same line-building structure, but hard-codes status `interrupted` and result `<interrupted before completion>`.

The behavior is nearly the same: both render `ToolTraceEntry` sequences into model-visible prompt text with the same indentation and truncation marker.
The meaningful difference is status/result semantics for interrupted tools, so this is a small duplication candidate only if the project wants one renderer for all tool trace statuses.

Differences to preserve: completed trace rendering treats started events without a result as `<not yet returned>`, while interrupted replay must always emit `<interrupted before completion>`.

### 3. Compact JSON preview helpers are related but not worth consolidating now

`src/mindroom/tool_system/events.py:52-58` and `src/mindroom/approval_manager.py:106-116` both use `json.dumps(..., ensure_ascii=False, sort_keys=True)` for stable preview text/length.
The surrounding behaviors diverge: tool events return compact text for Matrix trace previews, while approvals sanitize arbitrary values, track sanitizer truncation, allocate per-argument budgets, and may return structured preview objects.

This is shared vocabulary rather than active harmful duplication.
Pulling it into a common helper would save little and risks coupling approval safety rules to tool-event display rules.

## Proposed Generalization

1. Export a small canonical marker predicate or regex from `mindroom.tool_system.events`, and make `response_runner._strip_visible_tool_markers` call `is_visible_tool_marker_line()` instead of owning `_VISIBLE_TOOL_MARKER_LINE_PATTERN`.
2. Optionally add a tiny `render_tool_trace_lines(events, status_override=None, interrupted_result=None)` helper only if another renderer appears; with only one near-duplicate interrupted renderer, no immediate refactor is required.
3. Leave compact JSON preview and truncation helpers local until there is a stronger shared contract for preview serialization.

## Risk/tests

The marker predicate refactor is low risk but should be covered by existing tests around `response_runner._strip_visible_tool_markers`, interrupted replay persistence, and `tests/test_tool_events.py` marker spacing cases.
The renderer refactor has higher prompt-regression risk because exact model-visible replay text is behavior; update `tests/test_interrupted_replay.py` and `tests/test_tool_events.py` together if it is attempted.
No production code was edited for this audit.
