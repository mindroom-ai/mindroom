## Summary

Top duplication candidate: `ThreadTagsTools._payload` and `ThreadTagsTools._context_error` repeat the same sorted JSON tool payload pattern used by several custom Matrix-facing tools.
The `tag_thread`, `untag_thread`, and `list_thread_tags` methods also repeat room/context/authorization/canonical-thread resolution flows that are already partially centralized in `attachment_helpers.py` and used by `thread_summary.py`.
No meaningful duplication was found for the thread-tag-specific serialization and include/exclude tag filtering helpers.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_serialized_tags	function	lines 32-34	related-only	model_dump(mode="json", exclude_none=True), ThreadTagRecord serialization	src/mindroom/thread_tags.py:439; src/mindroom/thread_tags.py:441; src/mindroom/custom_tools/thread_tags.py:34
_serialized_tags_for_output	function	lines 37-46	none-found	serialized_tags_for_output, include one requested tag, tag in serialized	none
_thread_matches_tag_filters	function	lines 49-61	none-found	include_tag exclude_tag, tag filters, tag not in tags	src/mindroom/custom_tools/thread_tags.py:367; src/mindroom/custom_tools/thread_tags.py:421
ThreadTagsTools	class	lines 64-438	duplicate-found	Toolkit custom tool _payload _context_error resolve room canonical thread	src/mindroom/custom_tools/thread_summary.py:19; src/mindroom/custom_tools/matrix_message.py:18; src/mindroom/custom_tools/matrix_room.py:50; src/mindroom/custom_tools/matrix_api.py:137; src/mindroom/custom_tools/attachments.py:60; src/mindroom/custom_tools/subagents.py:43
ThreadTagsTools.__init__	method	lines 67-71	related-only	Toolkit __init__ name tools registration	src/mindroom/custom_tools/thread_summary.py:22; src/mindroom/custom_tools/matrix_message.py:36; src/mindroom/custom_tools/matrix_room.py:50; src/mindroom/custom_tools/matrix_api.py:137
ThreadTagsTools._payload	method	lines 74-77	duplicate-found	def _payload status tool json.dumps sort_keys	src/mindroom/custom_tools/thread_summary.py:28; src/mindroom/custom_tools/matrix_message.py:42; src/mindroom/custom_tools/matrix_room.py:56; src/mindroom/custom_tools/matrix_api.py:143; src/mindroom/custom_tools/dynamic_tools.py:45; src/mindroom/custom_tools/attachments.py:60; src/mindroom/custom_tools/subagents.py:43
ThreadTagsTools._context_error	method	lines 80-84	duplicate-found	def _context_error tool context unavailable runtime path	src/mindroom/custom_tools/thread_summary.py:34; src/mindroom/custom_tools/matrix_message.py:55; src/mindroom/custom_tools/matrix_room.py:62; src/mindroom/custom_tools/matrix_api.py:149; src/mindroom/custom_tools/attachments.py:405; src/mindroom/custom_tools/subagents.py:52
ThreadTagsTools.tag_thread	async_method	lines 86-176	related-only	resolve_requested_room_id room_access_allowed normalize_tag_name resolve_canonical_tool_thread_target set_thread_tag	src/mindroom/custom_tools/thread_summary.py:52; src/mindroom/custom_tools/thread_summary.py:57; src/mindroom/custom_tools/thread_summary.py:67; src/mindroom/custom_tools/thread_summary.py:82; src/mindroom/custom_tools/subagents.py:324
ThreadTagsTools.untag_thread	async_method	lines 178-285	related-only	resolve_context_thread_id resolve_canonical_tool_thread_target remove_thread_tag ThreadTagsError	src/mindroom/custom_tools/attachment_helpers.py:62; src/mindroom/custom_tools/attachment_helpers.py:89; src/mindroom/thread_tags.py:815; src/mindroom/thread_tags.py:903
ThreadTagsTools.list_thread_tags	async_method	lines 287-438	related-only	resolve_requested_room_id room_access_allowed normalize tags list_tagged_threads get_thread_tags filters	src/mindroom/custom_tools/thread_summary.py:57; src/mindroom/custom_tools/thread_summary.py:67; src/mindroom/thread_tags.py:726; src/mindroom/thread_tags.py:979
```

## Findings

### 1. Repeated custom-tool JSON payload builder

`ThreadTagsTools._payload` builds `{"status": status, "tool": "thread_tags"}`, merges keyword fields, and returns `json.dumps(..., sort_keys=True)` at `src/mindroom/custom_tools/thread_tags.py:74`.
The same behavior appears in `ThreadSummaryTools._payload` at `src/mindroom/custom_tools/thread_summary.py:28`, `MatrixMessageTools._payload` at `src/mindroom/custom_tools/matrix_message.py:42`, `MatrixRoomTools._payload` at `src/mindroom/custom_tools/matrix_room.py:56`, `MatrixApiTools._payload` at `src/mindroom/custom_tools/matrix_api.py:143`, `DynamicTools._payload` at `src/mindroom/custom_tools/dynamic_tools.py:45`, `_attachment_tool_payload` at `src/mindroom/custom_tools/attachments.py:60`, and `_payload` in `src/mindroom/custom_tools/subagents.py:43`.
These are functionally the same structured tool response format with only the `tool` name varying.

Differences to preserve: some modules keep the helper as a class method/static method and some as a module-level helper.
`subagents.py` passes `tool_name` per call because multiple tool functions share a module-level helper.

### 2. Repeated custom-tool context-unavailable error shape

`ThreadTagsTools._context_error` returns an error payload with a fixed unavailable-context message at `src/mindroom/custom_tools/thread_tags.py:80`.
Equivalent helpers exist in `thread_summary.py:34`, `matrix_message.py:55`, `matrix_room.py:62`, and `matrix_api.py:149`.
`attachments.py:405` repeats the payload inline for each method, and `subagents.py:52` has a module-level version.

The behavior is duplicated because every custom tool checks `get_tool_runtime_context()` and returns a JSON error payload when the runtime path does not provide context.
The text is intentionally tool-specific, but the structure and status are the same.

### 3. Room/context/thread target validation is related and already partially centralized

`tag_thread`, `untag_thread`, and `list_thread_tags` all perform the same sequence: get runtime context, resolve or inherit `room_id`, enforce `room_access_allowed`, normalize thread identifiers with `resolve_canonical_tool_thread_target`, call a domain operation, and convert domain exceptions into structured payloads.
The shared primitives are already in `src/mindroom/custom_tools/attachment_helpers.py:31`, `src/mindroom/custom_tools/attachment_helpers.py:47`, `src/mindroom/custom_tools/attachment_helpers.py:62`, and `src/mindroom/custom_tools/attachment_helpers.py:89`.
`ThreadSummaryTools.set_thread_summary` follows the same room/canonical-thread path at `src/mindroom/custom_tools/thread_summary.py:52`, `src/mindroom/custom_tools/thread_summary.py:57`, `src/mindroom/custom_tools/thread_summary.py:67`, and `src/mindroom/custom_tools/thread_summary.py:82`.

This is related duplication, but not a strong refactor target inside this file alone because the existing shared helpers already cover the risky normalization rules.
The remaining repetition is mostly action-specific error payload assembly and tag-specific validation.

### 4. Thread-tag-specific helpers do not have active duplicates

`_serialized_tags_for_output` and `_thread_matches_tag_filters` appear only in `src/mindroom/custom_tools/thread_tags.py`.
`src/mindroom/thread_tags.py:439` has `_thread_tag_record_content`, which serializes a single `ThreadTagRecord` with the same Pydantic options as `_serialized_tags`, but it is used for Matrix room-state content rather than tool output.
That is related serialization behavior, not a duplicate API boundary: tool payloads serialize a mapping of tag names to records, while room-state writes serialize one record as Matrix event content.

## Proposed Generalization

Introduce a small helper in `src/mindroom/custom_tools/attachment_helpers.py` or a new focused `src/mindroom/custom_tools/payloads.py` module:

1. `tool_payload(tool_name: str, status: str, **kwargs: object) -> str` to centralize the sorted JSON response shape.
2. Optionally `tool_context_error(tool_name: str, label: str | None = None, action: str | None = None) -> str` if callers can keep their current user-facing message text.
3. Migrate only the repeated `_payload` helpers first, leaving action-specific `_error_payload` wrappers and request validation intact.
4. Keep `_serialized_tags_for_output` and `_thread_matches_tag_filters` local to `thread_tags.py`.

No broad refactor is recommended for `tag_thread`, `untag_thread`, or `list_thread_tags` beyond continuing to use the existing target-resolution helpers.

## Risk/tests

The main risk is changing exact JSON output for tool responses, especially key ordering, `tool` names, and context-error message text.
Tests should cover representative success and error payloads for `thread_tags`, `thread_summary`, `matrix_message`, and one module-level helper such as `attachments` or `subagents`.
For thread tags specifically, tests should verify tag serialization still omits `None`, room-wide list filtering preserves `tag`/`include_tag`/`exclude_tag` behavior, and context-unavailable responses keep the same schema.
