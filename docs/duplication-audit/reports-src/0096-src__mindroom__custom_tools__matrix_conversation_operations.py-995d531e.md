## Summary

Top duplication candidates:

1. `MatrixMessageOperations` and `MatrixRoomTools` both serialize room thread roots from `get_room_threads_page`, including thread metadata extraction and preview generation.
2. Interactive-question registration plus reaction-button creation is repeated in the message tool and post-response delivery effects.
3. Text send/edit delivery in this module is related to existing delivery helpers, but most behavior differences are intentional for model-facing tool semantics.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MatrixMessageOperationResult	class	lines 50-54	related-only	MatrixMessageOperationResult status fields _payload result dataclass	src/mindroom/custom_tools/matrix_message.py:42-53; src/mindroom/custom_tools/browser.py:982
MatrixMessageOperations	class	lines 57-801	related-only	MatrixMessageOperations matrix_message MatrixRoomTools dispatch_action send read react edit room threads	src/mindroom/custom_tools/matrix_message.py:18-312; src/mindroom/custom_tools/matrix_room.py:184-205
MatrixMessageOperations._result	method	lines 66-67	related-only	_result status ok error fields payload json	src/mindroom/custom_tools/matrix_message.py:42-53; src/mindroom/custom_tools/matrix_room.py:60-67
MatrixMessageOperations._send_matrix_text	async_method	lines 69-107	related-only	format_message_with_mentions send_message_result notify_outbound_message latest_thread_event_id ORIGINAL_SENDER_KEY	src/mindroom/delivery_gateway.py:523-574; src/mindroom/custom_tools/subagents.py:250-280; src/mindroom/hooks/sender.py:60-94
MatrixMessageOperations._maybe_add_interactive_question	async_method	lines 109-138	duplicate-found	parse_and_format_interactive register_interactive_question add_reaction_buttons interactive_metadata options_as_list	src/mindroom/post_response_effects.py:110-132; src/mindroom/delivery_gateway.py:768-790
MatrixMessageOperations._message_send_or_reply	async_method	lines 140-328	related-only	send_context_attachments _resolve_send_attachments send_file_message thread reply attachments error result	src/mindroom/custom_tools/attachments.py:1-260; src/mindroom/matrix/client_delivery.py:396-413
MatrixMessageOperations._message_react	async_method	lines 330-371	related-only	m.reaction m.annotation room_send ignore_unverified_devices key event_id	src/mindroom/interactive.py:714-745; src/mindroom/stop.py:369-406; src/mindroom/commands/config_confirmation.py:316-342
MatrixMessageOperations._message_read	async_method	lines 373-422	related-only	room_messages extract_visible_message trusted_visible_sender_ids get_thread_messages	src/mindroom/thread_summary.py:224; src/mindroom/bot_room_lifecycle.py:156; src/mindroom/matrix/client_thread_history.py:1081-1095
MatrixMessageOperations._build_edit_options	method	lines 425-445	none-found	edit_options can_edit body_preview message_preview event_id sender	none
MatrixMessageOperations._thread_reply_count	method	lines 448-459	duplicate-found	thread_reply_count m.relations m.thread count bool	src/mindroom/custom_tools/matrix_room.py:76-91
MatrixMessageOperations._thread_latest_activity_ts	method	lines 462-478	none-found	latest_activity_ts m.thread latest_event origin_server_ts	none
MatrixMessageOperations._serialize_thread_root	async_method	lines 480-522	duplicate-found	thread root serialize body_preview reply_count latest_activity_ts thread_root_body_preview	src/mindroom/custom_tools/matrix_room.py:341-358
MatrixMessageOperations._room_threads	async_method	lines 524-569	duplicate-found	get_room_threads_page RoomThreadsPageError thread_roots next_token trusted_visible_sender_ids	src/mindroom/custom_tools/matrix_room.py:316-365
MatrixMessageOperations._thread_read_payload	async_method	lines 571-596	related-only	get_thread_messages full_history edit_options to_dict recent_messages	src/mindroom/conversation_resolver.py:426; src/mindroom/matrix/conversation_cache.py:136-197
MatrixMessageOperations._message_thread_list	async_method	lines 598-619	related-only	thread-list thread_id required thread_read_payload	context fallback only in src/mindroom/custom_tools/matrix_conversation_operations.py:748-779
MatrixMessageOperations._message_edit	async_method	lines 621-703	related-only	clear_interactive_question parse_and_format_interactive edit_message_result add_reaction_buttons notify_outbound_message	src/mindroom/delivery_gateway.py:576-639; src/mindroom/streaming.py:814-948
MatrixMessageOperations.dispatch_action	async_method	lines 705-801	related-only	dispatch_action resolve_context_thread_id unsupported action if action	src/mindroom/custom_tools/matrix_room.py:184-205; src/mindroom/custom_tools/matrix_message.py:72-87
```

## Findings

### 1. Room thread-root serialization is duplicated

`src/mindroom/custom_tools/matrix_conversation_operations.py:448-522` extracts bundled Matrix thread metadata, builds a thread payload with `thread_id`, `sender`, `timestamp`, `body_preview`, and `reply_count`, and optionally includes `latest_activity_ts`.

`src/mindroom/custom_tools/matrix_room.py:76-91` duplicates the same `unsigned -> m.relations -> m.thread -> count` traversal for reply counts.
`src/mindroom/custom_tools/matrix_room.py:341-358` then builds nearly the same thread-root payload after calling the same `thread_root_body_preview` helper.

Why this is duplicated: both tools present paginated Matrix thread roots to model-facing callers using the same source API and the same preview helper.
Differences to preserve: `matrix_message` validates malformed root fields and includes `latest_activity_ts`; `matrix_room` currently emits a simpler payload and has additional transport exception handling around thread fetches.

### 2. Interactive-question registration flow is repeated

`src/mindroom/custom_tools/matrix_conversation_operations.py:109-138` parses an outbound message for interactive metadata, registers the event, and adds reaction buttons.
`src/mindroom/post_response_effects.py:110-132` performs the same registration plus button addition for final response delivery, after parsing happened in `src/mindroom/delivery_gateway.py:768-790`.

Why this is duplicated: both paths need to persist the same `InteractiveMetadata` and attach the same reaction buttons to a delivered Matrix event.
Differences to preserve: the message-tool path accepts raw text and decides whether to create a question; the post-response path receives already-parsed metadata from delivery.

### 3. Matrix send/edit delivery is related but not a clear duplication target

`src/mindroom/custom_tools/matrix_conversation_operations.py:69-107` and `src/mindroom/custom_tools/matrix_conversation_operations.py:621-703` share the general pattern used in `src/mindroom/delivery_gateway.py:523-639`, `src/mindroom/custom_tools/subagents.py:250-280`, and `src/mindroom/hooks/sender.py:60-94`: compute latest thread event, call `format_message_with_mentions`, deliver through `send_message_result` or `edit_message_result`, then notify `conversation_cache`.

This is real conceptual overlap, but the call sites differ in thread targeting, reply handling, original-sender metadata, tool traces, hook metadata, logging, room-mode behavior, and interactive parsing.
The lower-level `send_message_result` and `edit_message_result` helpers already centralize the Matrix API delivery pieces, so a new abstraction here would need care to avoid hiding important target semantics.

### 4. Reaction send payloads are related but intentionally caller-specific

`src/mindroom/custom_tools/matrix_conversation_operations.py:330-371` sends a single model-requested reaction and returns a tool payload.
The same `m.reaction` / `m.annotation` payload shape appears in `src/mindroom/interactive.py:714-745`, `src/mindroom/stop.py:369-406`, and `src/mindroom/commands/config_confirmation.py:316-342`.

The duplicated content construction is small, but response handling differs: interactive buttons are best-effort, stop buttons update tracking state and notify outbound cache, config confirmation logs each button, and the message tool returns structured success/error data.

## Proposed Generalization

1. Move thread-root metadata extraction into a focused helper, likely `src/mindroom/matrix/client_visible_messages.py` or a small neighboring Matrix thread presentation module.
2. Expose one async serializer such as `serialize_thread_root_preview(event, *, client, config, runtime_paths, trusted_sender_ids) -> dict[str, object] | None` that includes `reply_count` and optional `latest_activity_ts`.
3. Use that helper from both `MatrixMessageOperations._serialize_thread_root` and `MatrixRoomTools._threads`, preserving each tool's action names, error payload format, and fetch exception handling.
4. Add a tiny interactive helper, for example `register_interactive_delivery(...)`, that takes already-parsed `InteractiveMetadata`; keep `should_create_interactive_question` and parsing decisions at the call sites.
5. Do not generalize Matrix send/edit or reaction delivery yet beyond the helpers already present in `matrix.client_delivery`; the behavioral differences are larger than the duplicated code.

## Risk/tests

Main risk is changing model-facing JSON shape for `matrix_message` or `matrix_room`.
Tests should assert both tools preserve their existing keys, malformed thread roots are skipped where currently skipped, `latest_activity_ts` remains present only when valid, and `reply_count` still rejects booleans.

Interactive helper tests should cover that registration uses the delivered event ID and thread ID unchanged, and that reaction buttons are still added with the same option list.

No production code was edited.
