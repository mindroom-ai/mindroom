Summary: top duplication candidates are the Matrix thread tool wrapper flows in `src/mindroom/custom_tools/thread_tags.py`, plus repeated JSON payload/context-error helpers across Matrix custom tools.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ThreadSummaryTools	class	lines 19-131	duplicate-found	ThreadSummaryTools Toolkit name tools payload context_error canonical thread target set manual summary	src/mindroom/custom_tools/thread_tags.py:64; src/mindroom/custom_tools/matrix_api.py:137; src/mindroom/custom_tools/matrix_room.py:50; src/mindroom/custom_tools/matrix_message.py:35; src/mindroom/custom_tools/subagents.py:43
ThreadSummaryTools.__init__	method	lines 22-26	related-only	Toolkit __init__ name thread_summary tools self set_thread_summary	src/mindroom/custom_tools/thread_tags.py:67; src/mindroom/custom_tools/matrix_api.py:137; src/mindroom/custom_tools/matrix_room.py:50
ThreadSummaryTools._payload	method	lines 29-32	duplicate-found	payload status tool json.dumps sort_keys custom_tools	src/mindroom/custom_tools/thread_tags.py:73; src/mindroom/custom_tools/matrix_api.py:143; src/mindroom/custom_tools/matrix_room.py:56; src/mindroom/custom_tools/matrix_message.py:43; src/mindroom/custom_tools/subagents.py:43; src/mindroom/custom_tools/attachments.py:60
ThreadSummaryTools._context_error	method	lines 35-40	duplicate-found	context_error Tool runtime context unavailable payload custom_tools	src/mindroom/custom_tools/thread_tags.py:79; src/mindroom/custom_tools/matrix_api.py:149; src/mindroom/custom_tools/matrix_room.py:62; src/mindroom/custom_tools/matrix_message.py:59; src/mindroom/custom_tools/subagents.py:52; src/mindroom/custom_tools/attachments.py:405
ThreadSummaryTools.set_thread_summary	async_method	lines 42-131	duplicate-found	get_tool_runtime_context resolve_requested_room_id room_access_allowed resolve_canonical_tool_thread_target resolve_thread_root_event_id_for_client domain error payload	src/mindroom/custom_tools/thread_tags.py:86; src/mindroom/custom_tools/thread_tags.py:178; src/mindroom/custom_tools/attachment_helpers.py:31; src/mindroom/custom_tools/attachment_helpers.py:47; src/mindroom/custom_tools/attachment_helpers.py:89; src/mindroom/thread_summary.py:442
```

## Findings

1. `ThreadSummaryTools.set_thread_summary` duplicates the active thread-tool flow used by `ThreadTagsTools.tag_thread`.
   `thread_summary.py:52-103` and `thread_tags.py:95-147` both fetch the tool runtime context, resolve and validate `room_id`, enforce `room_access_allowed`, resolve a canonical thread root through `resolve_canonical_tool_thread_target`, return structured error payloads on each failure, assert a canonical thread ID, then call one domain writer.
   `thread_summary.py:105-131` and `thread_tags.py:149-176` also share the same final try/domain-call/error-payload/success-payload shape.
   Differences to preserve: summary uses `action="set"`, validates summary emptiness before thread resolution, passes `fail_closed_on_normalization_error=True`, catches `ThreadSummaryWriteError`, and returns `event_id`, `message_count`, and normalized summary.
   Tags uses `action="tag"`, validates tag metadata, catches `ThreadTagsError`, and returns serialized tag state.

2. `ThreadSummaryTools._payload` is a literal local variant of the same JSON tool payload helper repeated across custom tools.
   `thread_summary.py:29-32`, `thread_tags.py:73-77`, `matrix_api.py:143-147`, `matrix_room.py:56-60`, `matrix_message.py:43-46`, and `subagents.py:43-49` all build a dict with `status` and tool name, merge kwargs, then `json.dumps(..., sort_keys=True)`.
   Differences to preserve: most class methods bake in a fixed tool name, while `subagents.py:43-49` accepts `tool_name` because that module exposes multiple tool actions.

3. `ThreadSummaryTools._context_error` repeats the same context-unavailable payload pattern used by other custom tools.
   `thread_summary.py:35-40`, `thread_tags.py:79-84`, `matrix_api.py:149-154`, `matrix_room.py:62-67`, `matrix_message.py:59-63`, and attachment methods such as `attachments.py:405-410` produce a JSON error payload with only tool-specific wording and, in the summary case, `action="set"`.
   Differences to preserve: existing messages name each tool differently, and summary includes an action field while most base context errors do not.

4. `ThreadSummaryTools.__init__` only follows the standard Agno `Toolkit` registration shape.
   This is related boilerplate rather than meaningful duplicated behavior.
   The same pattern appears in `thread_tags.py:67-71`, `matrix_api.py:137-141`, and `matrix_room.py:50-54`, but each instance necessarily differs by tool name and registered functions.

## Proposed Generalization

A narrow helper could live in `src/mindroom/custom_tools/attachment_helpers.py` or a renamed custom-tool helper module, because that file already owns `resolve_requested_room_id`, `room_access_allowed`, and `resolve_canonical_tool_thread_target`.
The useful extraction would be a small JSON payload helper such as `custom_tool_payload(tool_name, status, **fields)` and possibly `custom_tool_context_error(tool_name, message, **fields)`.
For the thread wrapper flow, a broader extraction is not recommended yet because `thread_summary` and `thread_tags` already share the important target-resolution helpers, and their domain validation, exception types, payload fields, and normalization-error policy differ enough that a callback-heavy helper could obscure the behavior.

## Risk/tests

Risk is mostly payload compatibility: tool outputs are model-facing JSON and likely asserted in tests or relied on by prompts, so any helper must preserve exact keys, `sort_keys=True`, action fields, and error text.
If refactored, add focused tests for `set_thread_summary` covering missing context, invalid room ID, unauthorized room, missing thread context, normalization failure, summary validation, write failure, and successful payload fields.
Also keep or add comparison tests for `thread_tags` so shared room/thread target behavior remains unchanged.
