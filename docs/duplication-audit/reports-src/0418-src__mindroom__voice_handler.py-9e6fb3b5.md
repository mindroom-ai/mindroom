Summary: One modest duplication candidate exists around Matrix media download wrappers that convert downloaded bytes into Agno media objects.
The voice normalization cache and available-entity filtering have related patterns elsewhere, but their behavior is specific enough that no shared abstraction is recommended from this file alone.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_PreparedVoiceMessage	class	lines 41-45	none-found	prepared voice message dataclass source text attachment metadata	src/mindroom/inbound_turn_normalizer.py:58, src/mindroom/inbound_turn_normalizer.py:66, src/mindroom/dispatch_handoff.py:86
_NormalizedVoiceMessage	class	lines 49-53	none-found	normalized voice cache attachment_id transcribed_message	src/mindroom/inbound_turn_normalizer.py:158, src/mindroom/coalescing_batch.py:58, src/mindroom/attachments.py:555
_voice_cache_key	function	lines 61-68	none-found	voice cache key storage_path room_id event_id thread_id resolve	src/mindroom/matrix/message_content.py:23, src/mindroom/matrix/message_content.py:87, src/mindroom/attachments.py:489
_get_cached_voice_normalization	function	lines 71-79	related-only	OrderedDict move_to_end cache hit bounded cache	src/mindroom/matrix/message_content.py:107, src/mindroom/matrix/message_content.py:344, src/mindroom/approval_manager.py:76
_store_cached_voice_normalization	function	lines 82-90	related-only	OrderedDict move_to_end popitem last false max entries	src/mindroom/matrix/message_content.py:172, src/mindroom/matrix/message_content.py:344, src/mindroom/approval_manager.py:83
_finalize_inflight_voice_normalization_task	function	lines 93-109	related-only	add_done_callback task result CancelledError exception cleanup	src/mindroom/background_tasks.py:52, src/mindroom/orchestration/runtime.py:279, src/mindroom/knowledge/refresh_scheduler.py:163
_compute_normalized_voice_message	async_function	lines 112-153	none-found	download register transcribe audio event attachment normalized voice	src/mindroom/attachments.py:555, src/mindroom/inbound_turn_normalizer.py:158, src/mindroom/attachments.py:499
_normalize_voice_message	async_function	lines 156-188	related-only	inflight task cache asyncio shield create_task coalesce duplicate work	src/mindroom/background_tasks.py:21, src/mindroom/knowledge/registry.py:133, src/mindroom/api/knowledge.py:197
prepare_voice_message	async_function	lines 191-253	none-found	prepare voice source m.mentions m.relates_to format_message_with_mentions fallback	src/mindroom/inbound_turn_normalizer.py:158, src/mindroom/dispatch_handoff.py:196, src/mindroom/matrix/mentions.py:354
_handle_voice_message	async_function	lines 256-321	none-found	voice enabled download transcribe process transcription available entities prefix	src/mindroom/commands/parsing.py:109, src/mindroom/inbound_turn_normalizer.py:158, src/mindroom/coalescing.py:125
_download_audio	async_function	lines 324-333	duplicate-found	download_media_bytes Audio content mime_type download_image agno media	src/mindroom/matrix/image_handler.py:18, src/mindroom/attachments.py:499, src/mindroom/matrix/media.py:187
_transcribe_audio	async_function	lines 336-376	none-found	audio transcriptions httpx openai compatible stt api key	src/mindroom/tools/openai.py:117, src/mindroom/tools/groq.py:89, src/mindroom/matrix/provisioning.py:96
_process_transcription	async_function	lines 379-488	none-found	voice transcription normalizer available agents teams Agent arun sanitize mentions	src/mindroom/routing.py:34, src/mindroom/commands/parsing.py:109, src/mindroom/scheduling.py:1219
_get_available_entities_for_sender	async_function	lines 491-517	related-only	get_available_agents_for_sender_authoritative agent_name teams available names	src/mindroom/turn_policy.py:284, src/mindroom/routing.py:144, src/mindroom/custom_tools/config_manager.py:466
_sanitize_unavailable_mentions	function	lines 520-543	related-only	mention regex configured allowed strip unavailable mentions parse_mentions_in_text	src/mindroom/matrix/mentions.py:17, src/mindroom/matrix/mentions.py:81, src/mindroom/matrix/mentions.py:283
_sanitize_unavailable_mentions.<locals>._replace	nested_function	lines 533-541	related-only	regex replacement configured lower allowed lower preserve token strip at	src/mindroom/matrix/mentions.py:334, src/mindroom/matrix/mentions.py:139, src/mindroom/matrix/mentions.py:283
```

Findings:

1. Matrix media download wrappers are duplicated in shape.
   `src/mindroom/voice_handler.py:324` downloads Matrix audio bytes with `download_media_bytes`, returns `None` on failure, and wraps bytes plus `media_mime_type(event)` in `agno.media.Audio`.
   `src/mindroom/matrix/image_handler.py:18` performs the same Matrix-download-to-Agno-media wrapper for images, with image-specific MIME sniffing and mismatch logging.
   `src/mindroom/attachments.py:499` and `src/mindroom/attachments.py:523` also repeat the first half of this behavior by downloading Matrix media bytes before registering file/video/image attachments.
   The shared behavior is the Matrix media fetch and event MIME extraction path; image handling must preserve byte-signature MIME resolution, while audio currently keeps the declared MIME type.

Related-only observations:

1. `_get_cached_voice_normalization`, `_store_cached_voice_normalization`, and `_normalize_voice_message` use a small in-memory `OrderedDict` LRU plus in-flight task coalescing.
   `src/mindroom/matrix/message_content.py:107` and `src/mindroom/matrix/message_content.py:344` use another bounded `OrderedDict` cache for MXC text, but with TTL and durable cache integration.
   This is related cache mechanics, not the same behavior; extracting a generic cache would add type and policy complexity for only two different cache policies.

2. `_finalize_inflight_voice_normalization_task` resembles general task completion callbacks in `src/mindroom/background_tasks.py:52` and orchestration cleanup callbacks in `src/mindroom/orchestration/runtime.py:279`.
   The voice callback persists successful task results into a domain cache and only removes the matching in-flight task.
   That domain-specific side effect makes it related-only.

3. `_get_available_entities_for_sender` repeats the common conversion from available `MatrixID` values to configured agent names seen in `src/mindroom/routing.py:144` and `src/mindroom/custom_tools/config_manager.py:466`.
   Voice additionally separates agents from teams after an authoritative room/sender availability query.
   A helper could be useful if more call sites need the same agent/team split, but current overlap is small and not worth broadening the authorization API from this audit alone.

4. `_sanitize_unavailable_mentions` uses a mention regex and case-insensitive configured-name matching that overlaps conceptually with the scanner and resolver in `src/mindroom/matrix/mentions.py:17`, `src/mindroom/matrix/mentions.py:81`, and `src/mindroom/matrix/mentions.py:283`.
   The voice function deliberately preserves unknown mentions and strips only the leading `@` from configured-but-unavailable entities.
   That is a distinct post-processing operation rather than duplicate mention rendering.

Proposed generalization:

For the media-wrapper duplication, consider a tiny helper in `src/mindroom/matrix/media.py`, for example `download_agno_audio(client, event)` or a narrowly typed `download_audio_media(client, event)`.
It would call `download_media_bytes`, return `None` on failure, and construct `Audio(content=bytes, mime_type=media_mime_type(event))`.
Keep image MIME sniffing in `matrix/image_handler.py`, because that behavior is intentionally image-specific.

No refactor recommended for the cache/task, transcription, available-entity, or mention-sanitization paths.

Risk/tests:

If the audio media helper is extracted, tests should cover unencrypted audio download success, encrypted download failure propagation through `download_media_bytes`, and MIME propagation into the returned `Audio`.
Voice normalization tests should continue to verify that an audio event is downloaded only once per `(storage_path, room_id, event_id, thread_id)` cache key and that fallback content still carries `com.mindroom.voice_raw_audio_fallback`.
Mention tests should cover that configured but unavailable voice-normalized mentions lose only the leading `@`, while unknown human mentions remain unchanged.
