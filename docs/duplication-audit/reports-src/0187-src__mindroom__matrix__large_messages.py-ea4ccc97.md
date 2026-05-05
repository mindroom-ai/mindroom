## Summary

Top duplication candidate: Matrix MXC upload and encrypted file-payload assembly in `src/mindroom/matrix/large_messages.py` and `src/mindroom/matrix/client_delivery.py`.
The rest of the module mostly owns large-message-specific policy: preview metadata filtering, event-size limits, streaming-edit throttling, and JSON sidecar preview construction.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_is_passthrough_preview_key	function	lines 66-73	none-found	passthrough preview keys io.mindroom sidecar metadata	none
_is_passthrough_edit_wrapper_key	function	lines 76-78	none-found	passthrough edit wrapper io.mindroom sidecar-only metadata	none
_copy_preview_metadata	function	lines 81-83	none-found	copy preview metadata passthrough content keys	src/mindroom/matrix/visible_body.py:41
_copy_edit_wrapper_metadata	function	lines 86-90	none-found	copy edit wrapper metadata io.mindroom keys	src/mindroom/matrix/message_builder.py:498; src/mindroom/matrix/client_delivery.py:448
_copy_inline_streaming_preview_metadata	function	lines 93-95	not-a-behavior-symbol	inline alias copy preview metadata	none
_room_is_encrypted	function	lines 98-99	related-only	room encrypted client.rooms cached_room encrypted	src/mindroom/matrix/client_delivery.py:124; src/mindroom/matrix/client_delivery.py:135; src/mindroom/matrix/client_delivery.py:286
_add_sidecar_metadata	function	lines 102-124	related-only	io.mindroom.long_text file url encrypted sidecar metadata	src/mindroom/matrix/message_content.py:47; src/mindroom/matrix/message_content.py:61; src/mindroom/matrix/message_content.py:73
_calculate_event_size	function	lines 127-140	none-found	canonical json event size 2000 overhead Matrix limit	none
_is_edit_message	function	lines 143-147	related-only	m.new_content m.relates_to rel_type m.replace edit detection	src/mindroom/matrix/message_builder.py:498; src/mindroom/matrix/message_content.py:255; src/mindroom/matrix/visible_body.py:41
_is_nonterminal_stream_content	function	lines 150-152	none-found	stream status pending streaming nonterminal	none
_clear_oversized_nonterminal_streaming_edit_rate_limits	function	lines 155-157	not-a-behavior-symbol	test reset rate limits	tests/test_streaming_behavior.py:46
_prune_expired_oversized_nonterminal_streaming_edit_rate_limits	function	lines 160-167	none-found	prune expired monotonic rate limit dict interval	none
should_send_oversized_nonterminal_streaming_edit	function	lines 170-193	none-found	oversized nonterminal streaming edit throttle room original event	src/mindroom/streaming.py:790; tests/test_streaming_behavior.py:571
_build_nonterminal_streaming_edit_preview	function	lines 196-243	related-only	streaming edit preview m.new_content m.replace markdown_to_html sidecar	src/mindroom/matrix/client_delivery.py:448; src/mindroom/matrix/message_builder.py:498
_prefix_by_bytes	function	lines 246-258	none-found	UTF-8 byte prefix binary search max_bytes	none
_create_preview	function	lines 265-290	related-only	truncate preview continuation indicator max bytes	src/mindroom/matrix/stale_stream_cleanup.py:1306; src/mindroom/matrix/client_visible_messages.py:220; src/mindroom/interactive.py:355
_upload_text_as_mxc	async_function	lines 293-387	duplicate-found	upload text as mxc encrypt attachment client.upload data_provider file_info	src/mindroom/matrix/client_delivery.py:271; src/mindroom/matrix/media.py:167; src/mindroom/matrix/message_content.py:87
_upload_text_as_mxc.<locals>.data_provider	nested_function	lines 353-354	duplicate-found	data_provider io.BytesIO upload bytes client.upload	src/mindroom/matrix/client_delivery.py:317
_build_file_content	async_function	lines 390-411	related-only	build m.file content info filename preview sidecar	src/mindroom/matrix/client_delivery.py:357
_upload_content_json_sidecar	async_function	lines 414-421	none-found	json dumps content sidecar matrix_event_content_json upload	none
prepare_large_message	async_function	lines 424-539	related-only	prepare large Matrix message sidecar preview edit streaming upload	src/mindroom/matrix/client_delivery.py:154; src/mindroom/matrix/message_content.py:308
```

## Findings

### Duplicate Matrix upload/encryption wrapper

`src/mindroom/matrix/large_messages.py:293` and `src/mindroom/matrix/client_delivery.py:271` both implement the same core Matrix media-upload flow:

- Convert source data to bytes and build size/mimetype metadata.
- Detect whether the target room is encrypted from nio's room cache.
- For encrypted rooms, call `crypto.attachments.encrypt_attachment`, switch upload content type/name to encrypted payload values, and assemble Matrix encrypted file metadata with `url`, `key`, `iv`, `hashes`, `v`, `mimetype`, and `size`.
- Provide upload bytes through a nested `data_provider` returning `io.BytesIO`.
- Call `client.upload`, validate `nio.UploadResponse.content_uri`, fill the encrypted payload URL, and return an MXC URI plus payload metadata.

Differences to preserve:

- `large_messages.py:293` uploads text/JSON strings and returns the exact `file_info` shape consumed by `_add_sidecar_metadata`.
- `client_delivery.py:271` reads a local `Path`, returns `{"info": info}` plus optional `{"file": encrypted_file_payload}`, logs path-specific failures, and refuses unknown room encryption state.
- `large_messages.py:293` currently accepts a missing room cache as unencrypted, while `client_delivery.py:271` treats unknown room state as an error.
- `large_messages.py:293` chooses logical filenames by mimetype (`message.html`, `message-content.json`, `message.txt`), while `client_delivery.py:271` preserves the local filename.

### Related edit-envelope construction, not a direct duplicate

`src/mindroom/matrix/large_messages.py:196` and `src/mindroom/matrix/large_messages.py:510` manually build `m.replace` envelopes with `m.new_content` and `m.relates_to`.
`src/mindroom/matrix/message_builder.py:498` and `src/mindroom/matrix/client_delivery.py:448` also build edit envelopes.
This is related behavior, but not a clean duplicate because large-message edits intentionally keep preview/sidecar metadata split between the outer edit wrapper and inner `m.new_content`.
The existing generic builders copy replacement content more broadly and would need careful parameterization to avoid moving `io.mindroom.long_text` or stream-visible metadata to the wrong layer.

### Related preview truncation helpers, not a direct duplicate

`src/mindroom/matrix/large_messages.py:265` truncates by UTF-8 bytes and appends Matrix-large-message indicators.
Other preview helpers truncate by character count or normalize whitespace, including `src/mindroom/matrix/stale_stream_cleanup.py:1306`, `src/mindroom/matrix/client_visible_messages.py:220`, and `src/mindroom/interactive.py:355`.
The intent is similar, but byte-limit safety for Matrix event-size control is unique enough that a shared helper is not clearly justified.

## Proposed Generalization

Extract only the upload/encryption primitive if this duplication becomes worth refactoring.
A minimal location would be `src/mindroom/matrix/client_delivery.py` or a small new `src/mindroom/matrix/upload.py` helper that accepts bytes, mimetype, upload filename, room-id policy, and log context, then returns `(mxc_uri, encrypted_file_payload_or_none)`.
Keep sidecar metadata construction in `large_messages.py`, because it owns the `io.mindroom.long_text` contract and preview-size accounting.

No refactor is recommended for edit-envelope construction or preview truncation at this time.

## Risk/tests

Risks for any upload helper extraction:

- Encrypted-room handling can regress if unknown room-cache behavior is accidentally unified between normal file uploads and sidecar uploads.
- The returned metadata shape differs by caller, so tests must assert exact `file`, `url`, `info`, `mimetype`, `size`, and encrypted filename behavior.
- Sidecar hydration depends on the JSON upload preserving `application/json`, UTF-8 bytes, and the `message-content.json` filename convention.

Tests needing attention:

- `tests/test_large_messages_integration.py` for sidecar preview and edit behavior.
- `tests/test_send_file_message.py` for normal file upload behavior.
- `tests/test_message_content.py` for v2 sidecar hydration.
- `tests/test_streaming_behavior.py` for oversized nonterminal streaming edit throttling and preview behavior.
