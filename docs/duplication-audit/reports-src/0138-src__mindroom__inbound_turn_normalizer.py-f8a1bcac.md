## Summary

Top duplication candidates are limited and mostly local to inbound turn normalization.
`resolve_text_event` and `prepare_file_sidecar_text_event` both build the same `PreparedTextEvent` from Matrix event source resolution, differing only by the sidecar guard and accepted event type.
Media attachment registration in `register_batch_media_attachments`, `register_routed_attachment`, and attachment-history helpers overlaps in orchestration shape, but the lower-level persistence behavior is already centralized in `mindroom.attachments`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
TextNormalizationRequest	class	lines 50-53	not-a-behavior-symbol	TextNormalizationRequest dataclass event PreparedTextEvent	src/mindroom/turn_controller.py:629; src/mindroom/turn_controller.py:868
VoiceNormalizationRequest	class	lines 57-61	not-a-behavior-symbol	VoiceNormalizationRequest AudioMessageEvent room event	src/mindroom/turn_controller.py:1929
VoiceNormalizationResult	class	lines 65-69	not-a-behavior-symbol	VoiceNormalizationResult effective_thread_id PreparedTextEvent	src/mindroom/turn_controller.py:1937; src/mindroom/turn_controller.py:1945; src/mindroom/turn_controller.py:1952
BatchMediaAttachmentRequest	class	lines 73-78	not-a-behavior-symbol	BatchMediaAttachmentRequest media_events room_id thread_id	src/mindroom/turn_controller.py:1796
BatchMediaAttachmentResult	class	lines 82-86	not-a-behavior-symbol	BatchMediaAttachmentResult attachment_ids fallback_images	src/mindroom/turn_controller.py:1803; src/mindroom/turn_controller.py:1804
DispatchPayload	class	lines 90-96	not-a-behavior-symbol	DispatchPayload prompt model_prompt media attachment_ids	src/mindroom/turn_controller.py:117; src/mindroom/media_inputs.py:14
DispatchPayloadWithAttachmentsRequest	class	lines 100-109	not-a-behavior-symbol	DispatchPayloadWithAttachmentsRequest current_attachment_ids thread_history fallback_images	src/mindroom/turn_controller.py:1805
InboundTurnNormalizerDeps	class	lines 113-121	not-a-behavior-symbol	InboundTurnNormalizerDeps runtime logger storage_path runtime_paths conversation_resolver	src/mindroom/turn_controller.py:57; src/mindroom/turn_controller.py:629
InboundTurnNormalizer	class	lines 125-399	related-only	inbound normalization voice sidecar media attachment dispatch payload	src/mindroom/turn_controller.py:629; src/mindroom/turn_controller.py:1786; src/mindroom/turn_controller.py:1910; src/mindroom/attachments.py:705
InboundTurnNormalizer._client	method	lines 130-135	related-only	client is None Matrix client ready RuntimeError	self module only; src/mindroom/turn_controller.py:899; src/mindroom/turn_controller.py:1724
InboundTurnNormalizer.resolve_text_event	async_method	lines 137-156	duplicate-found	resolve_visible_event_source PreparedTextEvent fallback_body server_timestamp	src/mindroom/inbound_turn_normalizer.py:207; src/mindroom/matrix/client_visible_messages.py:155; src/mindroom/turn_controller.py:629
InboundTurnNormalizer.prepare_voice_event	async_method	lines 158-205	related-only	prepare_voice_message derive_conversation_context build_message_target source_kind voice	src/mindroom/voice_handler.py:191; src/mindroom/turn_controller.py:1910; src/mindroom/conversation_resolver.py:158
InboundTurnNormalizer.prepare_file_sidecar_text_event	async_method	lines 207-228	duplicate-found	is_v2_sidecar_text_preview resolve_visible_event_source PreparedTextEvent	src/mindroom/inbound_turn_normalizer.py:137; src/mindroom/turn_controller.py:1970; src/mindroom/turn_controller.py:575
InboundTurnNormalizer.register_routed_attachment	async_method	lines 230-253	related-only	register_matrix_media_attachment routed media attachment is_matrix_media_dispatch_event	src/mindroom/turn_controller.py:1085; src/mindroom/attachments.py:705; src/mindroom/attachments.py:736
InboundTurnNormalizer.register_batch_media_attachments	async_method	lines 255-328	related-only	register media attachments image fallback download_image file video timing	src/mindroom/attachments.py:499; src/mindroom/attachments.py:523; src/mindroom/attachments.py:705; src/mindroom/attachments.py:762; src/mindroom/turn_controller.py:1786
InboundTurnNormalizer.register_batch_media_attachments.<locals>.emit_registration_timing	nested_function	lines 267-279	related-only	emit_elapsed_timing attachment_count fallback_image_count outcome	src/mindroom/attachments.py:807; src/mindroom/attachment_media.py:106; src/mindroom/turn_controller.py:686
InboundTurnNormalizer.build_dispatch_payload_with_attachments	async_method	lines 330-389	related-only	resolve_thread_attachment_ids parse_attachment_ids_from_thread_history merge_attachment_ids resolve_attachment_media MediaInputs	src/mindroom/turn_controller.py:1696; src/mindroom/turn_controller.py:1805; src/mindroom/attachment_media.py:72; src/mindroom/attachments.py:793
InboundTurnNormalizer._as_file_or_video_dispatch_event	method	lines 392-399	related-only	is_file_or_video_message_event TypeError Expected file or video	src/mindroom/matrix/media.py:59; src/mindroom/attachments.py:724; src/mindroom/matrix/media.py:69
```

## Findings

### 1. Text-event normalization is duplicated between normal text and sidecar file preview paths

`InboundTurnNormalizer.resolve_text_event` builds a `PreparedTextEvent` from `resolve_visible_event_source`, preserving sender, event ID, resolved body/source, and integer server timestamp at `src/mindroom/inbound_turn_normalizer.py:137`.
`InboundTurnNormalizer.prepare_file_sidecar_text_event` performs the same source resolution and `PreparedTextEvent` construction at `src/mindroom/inbound_turn_normalizer.py:207`, after guarding with `is_v2_sidecar_text_preview`.
The behavior is functionally the same once the sidecar guard passes.
The differences to preserve are that `resolve_text_event` accepts and returns existing `PreparedTextEvent` instances unchanged, while `prepare_file_sidecar_text_event` returns `None` for non-sidecar file events.

### 2. Media attachment registration orchestration repeats lower-level attachment dispatch shape, but not enough to justify another abstraction now

`register_batch_media_attachments` branches image vs file/video, calls `register_matrix_media_attachment`, accumulates attachment IDs, and treats failed image registration as an inline-image fallback at `src/mindroom/inbound_turn_normalizer.py:255`.
`register_routed_attachment` performs the same single-event registration call and ID extraction at `src/mindroom/inbound_turn_normalizer.py:230`.
The underlying image/file/video dispatch is already centralized in `register_matrix_media_attachment` at `src/mindroom/attachments.py:705`, with file/video and image persistence centralized at `src/mindroom/attachments.py:499` and `src/mindroom/attachments.py:523`.
Thread-history registration also loops over media events and extracts attachment IDs at `src/mindroom/attachments.py:762`.
These paths are related, but not a clear duplication target because they intentionally differ in error behavior: routed attachments log and skip, batch file/video failures raise, batch image failures can fall back to inline image media, and history registration deduplicates silently.

### 3. Dispatch payload assembly overlaps with call-site preparation but centralizes the meaningful behavior

`build_dispatch_payload_with_attachments` resolves current, thread-root, historical metadata, and historical media attachment IDs, merges them, resolves Agno media, appends fallback images, and returns `DispatchPayload` at `src/mindroom/inbound_turn_normalizer.py:330`.
The turn-controller call site gathers current message IDs and batch media IDs before invoking this method at `src/mindroom/turn_controller.py:1696` and `src/mindroom/turn_controller.py:1786`.
This is related setup rather than actionable duplication: the controller owns per-turn policy/context and the normalizer owns payload assembly.
The attachment-to-media conversion is already centralized in `resolve_attachment_media` at `src/mindroom/attachment_media.py:72`.

## Proposed Generalization

For the real duplication, add a private helper in `src/mindroom/inbound_turn_normalizer.py` if production edits are later allowed:

1. Add `_prepare_visible_text_event(event: nio.RoomMessageText | FileMessageEvent) -> PreparedTextEvent` that wraps `resolve_visible_event_source` and `PreparedTextEvent` construction.
2. Make `resolve_text_event` return existing `PreparedTextEvent` unchanged, otherwise call the helper.
3. Make `prepare_file_sidecar_text_event` keep the sidecar guard, then call the helper.
4. Keep media registration as-is; no refactor recommended until a third path needs the same fallback/error policy.
5. Cover with focused tests for normal text events, sidecar file previews, and unchanged `PreparedTextEvent` passthrough.

## Risk/tests

The text helper is low risk if it exactly preserves the integer-only `server_timestamp` normalization and visible-source resolution arguments.
Tests should assert the prepared event fields for both text and sidecar file preview events, plus `None` for non-sidecar file events.
Media registration should not be generalized without tests covering image fallback behavior, file/video failure raising, routed-media skip logging, and history deduplication because those policies intentionally diverge.
