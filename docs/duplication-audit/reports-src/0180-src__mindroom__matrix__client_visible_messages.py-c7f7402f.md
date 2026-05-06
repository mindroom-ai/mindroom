## Summary

Top duplication candidates:

1. Bundled replacement extraction is duplicated between `client_visible_messages.py` and `client_thread_history.py`, with slightly different candidate ordering and validation.
2. Best-effort room-message fallback body extraction is duplicated between `client_visible_messages.py` and `client_thread_history.py`.
3. Visible edit projection appears in both `client_visible_messages.py` and `conversation_cache.py`, but the target data shapes differ enough that only a small shared helper is warranted.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ResolvedVisibleMessage	class	lines 26-132	related-only	ResolvedVisibleMessage dataclass synthetic from_message_data to_dict visible_event_id	src/mindroom/matrix/cache/thread_history_result.py:25; src/mindroom/matrix/thread_projection.py:24; src/mindroom/matrix/stale_stream_cleanup.py:102
ResolvedVisibleMessage.from_message_data	method	lines 39-57	related-only	from_message_data extract_and_resolve_message ResolvedVisibleMessage	src/mindroom/matrix/client_thread_history.py:735; src/mindroom/matrix/stale_stream_cleanup.py:999; src/mindroom/matrix/message_content.py:192
ResolvedVisibleMessage.synthetic	method	lines 60-81	related-only	ResolvedVisibleMessage.synthetic synthetic visible message src/mindroom/matrix/client_thread_history.py:180; src/mindroom/matrix/client_thread_history.py:750; src/mindroom/matrix/stale_stream_cleanup.py:114; src/mindroom/api/openai_compat.py:695
ResolvedVisibleMessage.refresh_stream_status	method	lines 83-85	related-only	stream_status STREAM_STATUS_KEY refresh_stream_status	src/mindroom/matrix/client_thread_history.py:192; src/mindroom/matrix/client_thread_history.py:762; src/mindroom/matrix/stale_stream_cleanup.py:633
ResolvedVisibleMessage.apply_edit	method	lines 87-104	duplicate-found	apply edit latest_event_id extract_edit_body merged_content	src/mindroom/matrix/conversation_cache.py:228; src/mindroom/matrix/client_visible_messages.py:420; src/mindroom/matrix/thread_projection.py:263
ResolvedVisibleMessage.visible_event_id	method	lines 107-109	related-only	visible_event_id latest_visible_thread_event_id latest_event_id	src/mindroom/matrix/cache/thread_cache_helpers.py:23; src/mindroom/matrix/stale_stream_cleanup.py:630; src/mindroom/matrix/thread_projection.py:28
ResolvedVisibleMessage.reply_to_event_id	method	lines 112-114	duplicate-found	reply_to_event_id m.in_reply_to EventInfo	src/mindroom/matrix/event_info.py:161; src/mindroom/matrix/stale_stream_cleanup.py:675
ResolvedVisibleMessage.to_dict	method	lines 116-132	none-found	to_dict msgtype stream_status latest_event_id	none
trusted_visible_sender_ids	function	lines 135-140	related-only	active_internal_sender_ids trusted_sender_ids	src/mindroom/matrix/conversation_cache.py:412; src/mindroom/matrix/stale_stream_cleanup.py:1047; src/mindroom/authorization.py:83
_resolved_trusted_sender_ids	function	lines 143-151	none-found	trusted_sender_ids None active_internal_sender_ids runtime config	none
extract_visible_message	async_function	lines 154-171	related-only	extract_visible_message extract_and_resolve_message trusted_sender_ids	src/mindroom/matrix/message_content.py:192; src/mindroom/matrix/client_thread_history.py:727; src/mindroom/matrix/stale_stream_cleanup.py:994
extract_visible_edit_body	async_function	lines 174-191	related-only	extract_visible_edit_body extract_edit_body trusted_sender_ids	src/mindroom/matrix/message_content.py:255; src/mindroom/edit_regenerator.py:12; src/mindroom/matrix/conversation_cache.py:249
resolve_visible_event_source	async_function	lines 194-217	related-only	resolve_event_source_content visible_body_from_event_source normalized event source	src/mindroom/matrix/message_content.py:285; src/mindroom/matrix/visible_body.py:78; src/mindroom/matrix/client_thread_history.py:741
message_preview	function	lines 220-227	related-only	compact preview join split max_length truncate preview	src/mindroom/interactive.py:355; src/mindroom/approval_manager.py:106; src/mindroom/custom_tools/matrix_api.py:371; src/mindroom/thread_summary.py:121
_bundled_replacement_candidates	function	lines 230-252	duplicate-found	m.relations m.replace latest_event event bundled_replacement	src/mindroom/matrix/client_thread_history.py:224; src/mindroom/custom_tools/matrix_conversation_operations.py:448; src/mindroom/custom_tools/matrix_room.py:77
bundled_replacement_body	async_function	lines 255-280	related-only	bundled_replacement_body bundled_visible_body_preview resolve_event_source_content	src/mindroom/matrix/client_thread_history.py:286; src/mindroom/matrix/visible_body.py:109; tests/test_matrix_room_tool.py:534
_event_fallback_body	function	lines 283-293	duplicate-found	room message fallback body event.body content body	src/mindroom/matrix/client_thread_history.py:157; src/mindroom/matrix/media.py:161
thread_root_body_preview	async_function	lines 296-332	related-only	thread_root_body_preview bundled replacement resolve visible event source preview	src/mindroom/custom_tools/matrix_conversation_operations.py:504; src/mindroom/custom_tools/matrix_room.py:349; src/mindroom/matrix/client_thread_history.py:718
_reply_to_event_id_from_content	function	lines 335-346	duplicate-found	m.relates_to m.in_reply_to event_id reply_to_event_id	src/mindroom/matrix/event_info.py:161; src/mindroom/matrix/stale_stream_cleanup.py:675
replace_visible_message	function	lines 349-369	none-found	replace_visible_message dataclasses.replace body content coherent	none
_stream_status_from_content	function	lines 372-377	none-found	STREAM_STATUS_KEY stream_status content.get	none
_record_latest_thread_edit	function	lines 380-398	related-only	record latest edit timestamp event_id original_event_id m.replace	src/mindroom/matrix/thread_projection.py:263; src/mindroom/matrix/conversation_cache.py:245; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:100
_apply_latest_edits_to_messages	async_function	lines 401-450	duplicate-found	apply latest edits messages_by_event_id extract_edit_body synthesize missing originals	src/mindroom/matrix/conversation_cache.py:228; src/mindroom/matrix/client_thread_history.py:315; src/mindroom/matrix/stale_stream_cleanup.py:980
resolve_latest_visible_messages	async_function	lines 453-496	related-only	resolve latest visible messages record edits extract messages sender filter	src/mindroom/matrix/client_thread_history.py:256; src/mindroom/matrix/stale_stream_cleanup.py:479; src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:95
```

## Findings

### 1. Bundled replacement relation extraction is duplicated

`src/mindroom/matrix/client_visible_messages.py:230` walks `unsigned` and the top-level event source, then looks under `m.relations` / `m.replace` for `latest_event`, `event`, and the replacement object itself.
`src/mindroom/matrix/client_thread_history.py:224` independently walks `unsigned.m.relations.m.replace` and returns a bundled replacement source after validating it parses as a visible room message.

These are functionally the same Matrix relation traversal: both recover a bundled `m.replace` event that the homeserver already attached to a root event.
The differences to preserve are important:

- `client_visible_messages.py` also checks top-level `m.relations`, not only `unsigned`.
- `client_visible_messages.py` prefers `latest_event` before `event`; `client_thread_history.py` currently prefers `event` before `latest_event`.
- `client_thread_history.py` validates candidates with `_parse_visible_text_message_event`; `client_visible_messages.py` accepts raw mapping candidates and later asks `bundled_visible_body_preview` for a visible body.

### 2. Room-message fallback body extraction is duplicated

`src/mindroom/matrix/client_visible_messages.py:283` returns `event.body` for text/notice events, otherwise falls back to `event.source["content"]["body"]` when it is a string.
`src/mindroom/matrix/client_thread_history.py:157` implements the same behavior under `_room_message_fallback_body`.

The behavior is identical except for naming and docstring wording.
Both helpers also rely on their module-local `_VISIBLE_ROOM_MESSAGE_EVENT_TYPES` tuple.

### 3. Visible edit projection logic is repeated across message and event-source shapes

`src/mindroom/matrix/client_visible_messages.py:401` resolves the latest `m.replace` event with `extract_edit_body`, applies it to an existing `ResolvedVisibleMessage`, and can synthesize a missing original when the edit belongs to the required thread.
`src/mindroom/matrix/conversation_cache.py:228` resolves the latest cached edit for one original event source with `extract_edit_body`, merges edited content over the original content, updates the timestamp from `origin_server_ts`, and returns an event-source dict.

The shared behavior is "given an original message and the latest edit source, compute the latest visible body/content/timestamp".
The target shapes differ: one mutates `ResolvedVisibleMessage` instances and may synthesize originals; the other returns a Matrix event source suitable for `nio.RoomGetEventResponse`.
That makes a broad merge risky, but a small shared pure merge helper would reduce repeated content-normalization rules.

### 4. Reply target extraction duplicates `EventInfo`

`src/mindroom/matrix/client_visible_messages.py:335` manually reads `content["m.relates_to"]["m.in_reply_to"]["event_id"]`.
`src/mindroom/matrix/event_info.py:161` performs the same relation extraction while analyzing a whole event source.

This duplication is narrow but real.
`ResolvedVisibleMessage.reply_to_event_id` only has a content payload, not a full event source, so it cannot directly call `EventInfo.from_event` without constructing a temporary event source.

## Proposed Generalization

1. Add a small Matrix relation helper near existing relation parsing, likely in `src/mindroom/matrix/event_info.py`, for extracting `m.in_reply_to.event_id` from a content mapping.
2. Add a shared bundled replacement candidate iterator in a Matrix helper module, preserving caller-controlled candidate ordering and whether top-level `m.relations` is considered.
3. Replace `_event_fallback_body` and `_room_message_fallback_body` with one shared helper, probably in `client_visible_messages.py` if that module remains the visible-message projection surface, or a tiny `matrix/event_body.py` helper if avoiding cross-imports.
4. If edit projection grows again, extract only the content merge/timestamp normalization into a pure helper; keep mutation/synthesis behavior local to `client_visible_messages.py`.

No broad architecture refactor recommended.

## Risk/tests

Risks are mainly Matrix compatibility details around bundled replacement ordering and validation.
Any refactor should cover bundled `m.replace` forms from both `unsigned.m.relations` and top-level `m.relations`, both `latest_event` and `event`, and replacement objects that are themselves event-like.

Tests to update or add:

- Thread history tests around bundled replacements in `tests/test_thread_history.py`.
- Thread preview tests in `tests/test_matrix_room_tool.py`.
- Message content tests in `tests/test_message_content.py` for reply extraction and sidecar-hydrated edit bodies.

Assumption: this audit intentionally did not edit production code.
