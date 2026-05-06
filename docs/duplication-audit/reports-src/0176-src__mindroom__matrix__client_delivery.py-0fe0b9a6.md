## Summary

Top duplication candidates for `src/mindroom/matrix/client_delivery.py`:

1. Matrix MXC upload preparation is duplicated across file attachments, large-message sidecars, and avatar uploads.
2. Direct Matrix `room_send` wrappers outside `client_delivery.py` repeat encryption policy, response normalization, and failure logging for reactions and custom approval/events, but their event types and cache side effects make this related rather than an immediate extraction target.
3. Matrix edit envelope construction is already mostly centralized through `build_matrix_edit_content`; no further duplication found for message edits.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
DeliveredMatrixEvent	class	lines 38-42	related-only	DeliveredMatrixEvent content_sent RoomSendResponse event_id notify_outbound_message	src/mindroom/thread_summary.py:429; src/mindroom/delivery_gateway.py:569; src/mindroom/streaming.py:904; src/mindroom/hooks/sender.py:92
_sanitized_delivery_error_message	function	lines 45-49	none-found	OlmTrustError sanitized delivery exception Matrix encrypted delivery rejected	src/mindroom/streaming.py:82; src/mindroom/streaming_delivery.py:41; src/mindroom/response_runner.py:1276
_log_matrix_delivery_exception	function	lines 52-67	none-found	matrix_message_delivery_exception delivery exception cache_bypass operation room_id	src/mindroom/custom_tools/matrix_api.py:828; src/mindroom/approval_transport.py:202; src/mindroom/interactive.py:746
_send_prepared_room_message	async_function	lines 70-121	related-only	room_send ignore_unverified_devices Api.room_send client._send cache_bypass	src/mindroom/approval_transport.py:185; src/mindroom/approval_transport.py:297; src/mindroom/approval_transport.py:385; src/mindroom/custom_tools/matrix_api.py:822; src/mindroom/custom_tools/matrix_conversation_operations.py:349; src/mindroom/stop.py:370; src/mindroom/interactive.py:734; src/mindroom/commands/config_confirmation.py:318
cached_room	function	lines 124-126	related-only	client.rooms Mapping cached_room MatrixRoom	src/mindroom/conversation_resolver.py:686; src/mindroom/custom_tools/matrix_room.py:215; src/mindroom/custom_tools/matrix_room.py:285; src/mindroom/matrix/presence.py:116
cached_rooms	function	lines 129-132	related-only	client.rooms isinstance Mapping cached_rooms	src/mindroom/matrix/presence.py:116; src/mindroom/conversation_resolver.py:686
_can_send_to_encrypted_room	function	lines 135-146	related-only	can_send_to_encrypted_room ENCRYPTION_ENABLED encrypted room ignore_unverified_devices	src/mindroom/approval_transport.py:176; src/mindroom/approval_transport.py:282; src/mindroom/approval_transport.py:376
can_send_to_encrypted_room	function	lines 149-151	not-a-behavior-symbol	public wrapper _can_send_to_encrypted_room	src/mindroom/approval_transport.py:176; src/mindroom/approval_transport.py:282; src/mindroom/approval_transport.py:376
send_message_result	async_function	lines 154-263	related-only	send_message_result room_send prepare_large_message RoomSendResponse matrix_message_sent	src/mindroom/delivery_gateway.py:564; src/mindroom/thread_summary.py:424; src/mindroom/custom_tools/subagents.py:275; src/mindroom/custom_tools/matrix_conversation_operations.py:98; src/mindroom/scheduling.py:883; src/mindroom/matrix/stale_stream_cleanup.py:193
_guess_mimetype	function	lines 266-268	duplicate-found	mimetypes.guess_type application/octet-stream content_type avatar	src/mindroom/matrix/avatar.py:14; src/mindroom/attachments.py:172
_upload_file_as_mxc	async_function	lines 271-342	duplicate-found	client.upload data_provider encrypt_attachment UploadResponse content_uri file_info	src/mindroom/matrix/large_messages.py:293; src/mindroom/matrix/avatar.py:31
_upload_file_as_mxc.<locals>.data_provider	nested_function	lines 317-318	duplicate-found	data_provider io.BytesIO upload_data upload_payload avatar_data	src/mindroom/matrix/large_messages.py:353; src/mindroom/matrix/avatar.py:56
_msgtype_for_mimetype	function	lines 345-354	none-found	msgtype mimetype image video audio m.file	src/mindroom/matrix/message_content.py:48; src/mindroom/matrix/large_messages.py:405
send_file_message	async_function	lines 357-417	none-found	send_file_message attachment filename m.relates_to m.thread conversation_cache	src/mindroom/custom_tools/attachments.py:264; src/mindroom/custom_tools/matrix_conversation_operations.py:229
build_threaded_edit_content	function	lines 420-445	related-only	format_message_with_mentions latest_thread_event_id required thread fallback edit	src/mindroom/custom_tools/matrix_conversation_operations.py:649; src/mindroom/matrix/message_builder.py:488
build_edit_event_content	function	lines 448-471	related-only	build_matrix_edit_content m.new_content m.replace formatted_body body star	src/mindroom/matrix/message_builder.py:498; src/mindroom/approval_transport.py:297; src/mindroom/streaming.py:785
edit_message_result	async_function	lines 474-492	none-found	edit_message_result build_edit_event_content operation edit_message	src/mindroom/delivery_gateway.py:617; src/mindroom/streaming.py:948; src/mindroom/custom_tools/matrix_conversation_operations.py:657; src/mindroom/matrix/stale_stream_cleanup.py:1135
```

## Findings

### 1. MXC upload setup is repeated for attachments, large messages, and avatars

`_upload_file_as_mxc` reads bytes, derives an upload info payload, optionally encrypts for encrypted rooms, wraps a `data_provider`, calls `client.upload`, unwraps nio's tuple response shape, validates `UploadResponse.content_uri`, and returns MXC payload data at `src/mindroom/matrix/client_delivery.py:271`.

The same upload mechanics appear in `_upload_text_as_mxc` at `src/mindroom/matrix/large_messages.py:293`, including encryption via `crypto.attachments.encrypt_attachment`, a Matrix file info payload with `key`, `iv`, `hashes`, `v`, `mimetype`, and `size`, a nested `data_provider` at `src/mindroom/matrix/large_messages.py:353`, and `client.upload` response validation at `src/mindroom/matrix/large_messages.py:358`.

Avatar upload repeats the non-encrypted subset at `src/mindroom/matrix/avatar.py:31`: read bytes, choose a content type, nested `data_provider` at `src/mindroom/matrix/avatar.py:56`, `client.upload`, tuple response unwrapping, `UploadResponse` validation, and missing-URI handling.

Differences to preserve:

- File attachments require known room cache state and place encrypted metadata under `content["file"]`, while large-message sidecars return the encrypted metadata as file info.
- Avatar uploads are profile media, not room media, so they should not use room encryption.
- Large-message sidecars choose synthetic filenames from MIME type; file attachments preserve the source filename.

### 2. Direct `room_send` delivery normalization is repeated outside normal message delivery

`_send_prepared_room_message` and `send_message_result` centralize standard `m.room.message` delivery, trust-policy handling, cache-bypass behavior, timing events, `RoomSendResponse` normalization, and sanitized local exception logging at `src/mindroom/matrix/client_delivery.py:70` and `src/mindroom/matrix/client_delivery.py:154`.

Several call sites still directly call `client.room_send` with similar response handling:

- Approval custom events and approval notices at `src/mindroom/approval_transport.py:185`, `src/mindroom/approval_transport.py:297`, and `src/mindroom/approval_transport.py:385`.
- Stop-button and interactive reactions at `src/mindroom/stop.py:370`, `src/mindroom/interactive.py:734`, and `src/mindroom/commands/config_confirmation.py:318`.
- Matrix conversation tool reactions at `src/mindroom/custom_tools/matrix_conversation_operations.py:349`.
- Generic Matrix API custom event sends at `src/mindroom/custom_tools/matrix_api.py:822`.

This is functionally related rather than a direct duplicate of `send_message_result`, because these paths send non-`m.room.message` event types, reaction events, or approval-specific event types and often have custom audit/cache side effects.

Differences to preserve:

- Approval events use `io.mindroom.tool_approval`, approval-specific thread relation lookup, and approval-cache writes.
- Reactions use `m.reaction`, not `m.room.message`, and their content is not large-message eligible.
- Generic Matrix API sends arbitrary event types and has separate audit/rate-limit behavior.

### 3. Edit envelope construction is sufficiently centralized

`build_edit_event_content` at `src/mindroom/matrix/client_delivery.py:448` delegates the core `m.replace` envelope to `build_matrix_edit_content` at `src/mindroom/matrix/message_builder.py:498`, then adds text-message fallback fields and optional extra content.

Approval edits also use `build_matrix_edit_content` directly at `src/mindroom/approval_transport.py:297`, which is appropriate because approval events are custom event types and should not receive text-message fallback fields.

No additional refactor is recommended here.

## Proposed Generalization

1. Extract a small `matrix/upload.py` helper for byte-backed uploads, for example `upload_bytes_as_mxc(client, payload, filename, content_type, *, room=None, original_mimetype=None)`.
2. Keep room encryption optional and explicit so avatar uploads can use the same upload/response normalization without room encryption.
3. Return a typed dataclass containing `content_uri`, `info`, and optional encrypted `file` metadata so callers can preserve their current content shapes.
4. Leave reaction/custom-event `room_send` paths alone unless future work needs consistent sanitized exception handling for all Matrix event types.

## Risk/tests

Upload consolidation is moderate risk because encrypted rooms require exact Matrix attachment metadata shape.
Tests should cover unencrypted file attachment upload, encrypted file attachment upload, large-message sidecar upload in encrypted and unencrypted rooms, upload tuple response unwrapping, missing `content_uri`, and avatar upload MIME fallback.

No production code was edited for this audit.
