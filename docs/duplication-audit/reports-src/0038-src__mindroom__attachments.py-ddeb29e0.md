## Summary

Top duplication candidates in `src/mindroom/attachments.py` are narrow and mostly helper-level.
The strongest candidates are ordered attachment-ID deduplication repeated across attachment metadata and runtime context, Matrix media event metadata extraction repeated between attachment registration and image handling, and MIME normalization duplicated with `src/mindroom/matrix/media.py`.
No broad cleanup/storage lifecycle duplication was found outside this module.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_normalize_attachment_id	function	lines 52-57	related-only	attachment_id strip validation att_ context availability	src/mindroom/tool_system/runtime_context.py:495, src/mindroom/custom_tools/attachments.py:161
AttachmentRecord	class	lines 61-90	related-only	AttachmentRecord attachment metadata dataclass payload	src/mindroom/attachment_media.py:20, src/mindroom/custom_tools/attachments.py:109
AttachmentRecord.to_payload	method	lines 76-90	related-only	to_payload attachments_for_tool_payload attachment json payload	src/mindroom/custom_tools/attachments.py:560, src/mindroom/custom_tools/attachments.py:665
parse_attachment_ids_from_event_source	function	lines 93-112	none-found	ATTACHMENT_IDS_KEY content attachment_ids parse event_source	src/mindroom/conversation_resolver.py:263, src/mindroom/coalescing_batch.py:190, src/mindroom/response_runner.py:111
parse_attachment_ids_from_thread_history	function	lines 115-125	duplicate-found	thread_history attachment_ids seen order parse_attachment_ids	src/mindroom/attachments.py:134, src/mindroom/tool_system/runtime_context.py:508, src/mindroom/inbound_turn_normalizer.py:345
_thread_history_message_in_scope	function	lines 128-131	related-only	thread_id message.event_id message.thread_id scope	src/mindroom/matrix/thread_membership.py:406, src/mindroom/matrix/client_visible_messages.py:369
merge_attachment_ids	function	lines 134-143	duplicate-found	merge attachment_ids seen preserve order runtime_attachment_ids	src/mindroom/tool_system/runtime_context.py:508, src/mindroom/inbound_turn_normalizer.py:353, src/mindroom/coalescing_batch.py:190
append_attachment_ids_prompt	function	lines 146-152	duplicate-found	Available attachment IDs Use tool calls inspect process	src/mindroom/inbound_turn_normalizer.py:371, src/mindroom/media_fallback.py:41
_attachments_dir	function	lines 155-156	none-found	storage_path attachments directory attachment record path	none
_incoming_media_dir	function	lines 159-160	none-found	storage_path incoming_media directory media storage	none
_attachment_record_path	function	lines 163-164	none-found	attachment record path attachments json	none
_extension_from_mime_type	function	lines 167-175	duplicate-found	mime_type split semicolon strip lower guess_extension normalize_mime_type	src/mindroom/matrix/media.py:130, src/mindroom/matrix/client_delivery.py:245
_store_media_bytes_locally	function	lines 178-197	related-only	persist media bytes incoming_media write_bytes download media	src/mindroom/matrix/image_handler.py:35, src/mindroom/matrix/client_delivery.py:284
_store_media_bytes_locally_async	async_function	lines 200-213	related-only	asyncio.to_thread store media bytes	src/mindroom/custom_tools/attachments.py:565, src/mindroom/tool_system/sandbox_proxy.py:620
_attachment_id_for_event	function	lines 216-219	related-only	sha256 event_id att_ digest stable id	src/mindroom/handled_turns.py:694, src/mindroom/mcp/manager.py:362
_record_mtime	function	lines 222-226	related-only	path.stat st_mtime datetime fromtimestamp UTC	src/mindroom/matrix/state.py:128, src/mindroom/approval_manager.py:103
_record_created_at	function	lines 229-236	related-only	datetime.fromisoformat tzinfo UTC fallback mtime	src/mindroom/approval_manager.py:103, src/mindroom/approval_events.py:151, src/mindroom/thread_tags.py:112
_is_managed_media_path	function	lines 239-244	related-only	resolve is_relative_to storage path containment	src/mindroom/workspaces.py:247, src/mindroom/tool_system/output_files.py:505
_collect_attachment_cleanup_state	function	lines 247-280	none-found	attachment cleanup state active ref counts expired stale	none
_remove_paths	function	lines 283-292	related-only	unlink missing_ok ignore OSError cleanup paths	src/mindroom/oauth/state.py:66, src/mindroom/matrix/invited_rooms_store.py:51
_prune_expired_records_and_collect_removable_media_paths	function	lines 295-312	none-found	expired attachment records removable media active ref counts	none
_prune_orphan_incoming_media	function	lines 315-341	none-found	orphan incoming_media active_media_paths cutoff	none
_cleanup_attachment_storage	function	lines 344-377	none-found	cleanup attachment storage retention expired orphan media	none
_maybe_cleanup_attachment_storage	function	lines 380-391	none-found	cleanup interval last_cleanup_time_by_storage_path	none
register_local_attachment	function	lines 394-452	related-only	register local attachment metadata atomic json tmp replace	src/mindroom/custom_tools/attachments.py:176, src/mindroom/oauth/state.py:105, src/mindroom/matrix/invited_rooms_store.py:51
_filename_for_media_event	function	lines 455-461	duplicate-found	content filename body media event filename extraction	src/mindroom/matrix/media.py:152, src/mindroom/matrix/client_delivery.py:245
_register_media_attachment	async_function	lines 464-496	related-only	persist media bytes register scoped attachment record	src/mindroom/inbound_turn_normalizer.py:230, src/mindroom/custom_tools/attachments.py:176
register_file_or_video_attachment	async_function	lines 499-520	related-only	download_media_bytes file video register media attachment	src/mindroom/inbound_turn_normalizer.py:309, src/mindroom/matrix/client_delivery.py:284
register_image_attachment	async_function	lines 523-552	duplicate-found	download image bytes resolve image mime mismatch warning	src/mindroom/matrix/image_handler.py:35, src/mindroom/inbound_turn_normalizer.py:289
register_audio_attachment	async_function	lines 555-577	related-only	audio bytes register attachment voice handler	src/mindroom/voice_handler.py:215, src/mindroom/matrix/media.py:64
load_attachment	function	lines 580-624	related-only	json metadata load validate dataclass record	src/mindroom/custom_tools/attachments.py:109, src/mindroom/oauth/state.py:75, src/mindroom/matrix/invited_rooms_store.py:32
resolve_attachments	function	lines 627-639	duplicate-found	resolve attachment ids preserving order load missing skip	src/mindroom/custom_tools/attachments.py:70, src/mindroom/tool_system/runtime_context.py:508
filter_attachments_for_context	function	lines 642-667	related-only	filter records room_id thread_id rejected	src/mindroom/attachment_media.py:87, src/mindroom/custom_tools/attachment_helpers.py:1
_load_existing_context_attachment	function	lines 670-685	related-only	existing attachment record room thread local_path is_file	src/mindroom/custom_tools/attachments.py:109, src/mindroom/attachment_media.py:87
_media_event_from_thread_history_message	function	lines 688-702	duplicate-found	thread history message content event source parse media event	src/mindroom/matrix/media.py:74, src/mindroom/conversation_resolver.py:231
register_matrix_media_attachment	async_function	lines 705-733	related-only	dispatch image file video register media attachment	src/mindroom/inbound_turn_normalizer.py:230, src/mindroom/inbound_turn_normalizer.py:295
_register_thread_history_media_attachment	async_function	lines 736-759	related-only	existing record then register matrix media attachment	src/mindroom/attachments.py:839, src/mindroom/inbound_turn_normalizer.py:230
register_thread_history_media_attachments	async_function	lines 762-790	duplicate-found	thread history media attachment ids seen order parse media	register_thread_history_media_attachments internal lines 771-789, src/mindroom/tool_system/runtime_context.py:508
resolve_thread_attachment_ids	async_function	lines 793-863	related-only	resolve thread root attachment ids event metadata media root existing record timing	src/mindroom/inbound_turn_normalizer.py:335, src/mindroom/matrix/image_handler.py:35
resolve_thread_attachment_ids.<locals>.finish	nested_function	lines 810-820	related-only	emit_elapsed_timing nested finish attachment_count outcome	src/mindroom/inbound_turn_normalizer.py:267, src/mindroom/attachment_media.py:106
attachments_for_tool_payload	function	lines 866-873	related-only	attachment record payload available local_path is_file tool payload	src/mindroom/custom_tools/attachments.py:60, src/mindroom/custom_tools/attachments.py:560
```

## Findings

### 1. Ordered attachment-ID deduplication is repeated

`parse_attachment_ids_from_thread_history` in `src/mindroom/attachments.py:115`, `merge_attachment_ids` in `src/mindroom/attachments.py:134`, `register_thread_history_media_attachments` in `src/mindroom/attachments.py:762`, and `list_tool_runtime_attachment_ids` in `src/mindroom/tool_system/runtime_context.py:508` all build a first-seen ordered list while skipping empty or already-seen attachment IDs.
The call-site inputs differ, but the core behavior is the same: preserve stable context order while avoiding duplicate IDs.
`resolve_attachments` in `src/mindroom/attachments.py:627` repeats the same normalization-plus-dedupe shape before loading records.

Differences to preserve:
`parse_attachment_ids_from_event_source` validates IDs through `_normalize_attachment_id`, while runtime context helpers currently only strip and skip empties.
`resolve_attachments` needs to load records and skip missing metadata, not just dedupe strings.

### 2. Attachment prompt text is duplicated

`append_attachment_ids_prompt` in `src/mindroom/attachments.py:146` appends the exact model-facing wording for available attachment IDs.
`InboundTurnNormalizer.build_dispatch_payload_with_attachments` repeats the same sentence in `src/mindroom/inbound_turn_normalizer.py:371`.
The behavior is functionally the same, but one produces a full prompt string and the other produces a separate `model_prompt`.

Differences to preserve:
The normalizer intentionally returns `None` when there are no resolved IDs and does not mutate `request.prompt`.

### 3. MIME normalization is split between attachment storage and Matrix media helpers

`_extension_from_mime_type` normalizes MIME input with `split(";", 1)`, `strip()`, and `lower()` in `src/mindroom/attachments.py:167`.
`_normalize_mime_type` performs the same normalization in `src/mindroom/matrix/media.py:130`.
The former maps missing/unknown MIME types to `.bin`; the latter returns `None` for invalid or empty normalized values.

Differences to preserve:
`_extension_from_mime_type` must keep the stable `.bin` fallback for local persisted file names.
`resolve_image_mime_type` must keep returning `None` for absent MIME values so image MIME mismatch handling remains accurate.

### 4. Matrix media filename/caption extraction overlaps

`_filename_for_media_event` in `src/mindroom/attachments.py:455` reads Matrix media `content["filename"]` and falls back to `event.body`.
`extract_media_caption` in `src/mindroom/matrix/media.py:152` reads the same fields but uses them to decide whether body is a user caption or just a filename.
`src/mindroom/matrix/client_delivery.py:245` also needs file names/MIME metadata while sending local files.

Differences to preserve:
Attachment registration wants a best-effort filename and may use the body as fallback.
Caption extraction must avoid treating a filename-like body as a caption.

### 5. Image media download and MIME mismatch handling is duplicated

`register_image_attachment` in `src/mindroom/attachments.py:523` downloads image bytes when not provided, resolves effective MIME type, logs mismatches, and registers the attachment.
`download_image` in `src/mindroom/matrix/image_handler.py:18` downloads bytes, resolves the same MIME metadata, logs nearly the same mismatch, and builds an Agno `Image`.
`InboundTurnNormalizer.register_batch_media_attachments` in `src/mindroom/inbound_turn_normalizer.py:289` calls `download_image` and then passes those same bytes to `register_matrix_media_attachment`.

Differences to preserve:
`download_image` returns Agno media for inline fallback.
`register_image_attachment` persists the bytes and metadata, and accepts pre-downloaded bytes to avoid a second Matrix download.

### 6. Thread-history media source reconstruction is close to Matrix media parsing

`_media_event_from_thread_history_message` in `src/mindroom/attachments.py:688` rebuilds a Matrix event source from `ResolvedVisibleMessage`, then delegates to `parse_matrix_media_dispatch_event_source` in `src/mindroom/matrix/media.py:74`.
The duplicate behavior is the construction of minimal Matrix event-source dictionaries from visible message fields, a pattern also adjacent to event-source handling in `src/mindroom/conversation_resolver.py:231`.

Differences to preserve:
Thread-history reconstruction must force `type: "m.room.message"` and include the room ID supplied by the caller.

## Proposed Generalization

1. Add a small ordered-ID helper only if another attachment-adjacent call site is touched, for example `unique_attachment_ids(ids: Iterable[str], normalize: bool = False) -> list[str]` in `src/mindroom/attachments.py` or a neutral utility.
2. Expose a `format_attachment_ids_prompt(attachment_ids: Sequence[str]) -> str | None` helper so the normalizer and `append_attachment_ids_prompt` share one wording source.
3. Consider making `normalize_mime_type` public in `src/mindroom/matrix/media.py` and using it inside `_extension_from_mime_type`.
4. Keep image download and registration split, but extract a small shared `resolve_image_payload_mime(event, media_bytes, log_label)` helper if mismatch logging changes again.
5. No refactor is recommended for attachment cleanup, storage directory helpers, or attachment JSON metadata loading until a second active storage implementation appears.

## Risk/tests

Risks are mostly ordering and compatibility risks: attachment IDs must remain first-seen ordered, invalid IDs must continue to be rejected in event metadata paths, and thread scoping must not leak attachments across rooms or threads.
Relevant tests to run for any future refactor are `uv run pytest tests/test_attachments.py tests/test_attachments_tool.py tests/test_matrix_message_tool.py -x -n 0 --no-cov -v`.
For MIME/image changes, also run tests covering `src/mindroom/matrix/image_handler.py` and inbound media normalization.
