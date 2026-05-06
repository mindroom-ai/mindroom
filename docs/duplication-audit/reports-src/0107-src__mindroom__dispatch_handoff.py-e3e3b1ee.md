# Duplication Audit: `src/mindroom/dispatch_handoff.py`

## Summary

Top duplication candidates:

1. `dispatch_handoff._event_content_dict` duplicates the same `event.source` content-dict extraction in `coalescing_batch._event_content_dict`, with many related local variants across Matrix content modules.
2. Dispatch payload overlay/reconstruction for mentions, formatted HTML, skip-mentions, attachment IDs, original sender, and raw-audio fallback is split between `dispatch_handoff.py`, `conversation_resolver.py`, `turn_controller.py`, and `coalescing_batch.py`.
3. Mention extraction from `m.mentions.user_ids` is duplicated in `dispatch_handoff.py` and overlaps with richer mention parsing in `thread_utils.py`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_PendingEventLike	class	lines 34-37	related-only	PendingEvent event source_kind trust_internal_payload_metadata protocol	src/mindroom/coalescing_batch.py:25
PreparedTextEvent	class	lines 41-50	related-only	PreparedTextEvent source_kind_override normalized text event	src/mindroom/inbound_turn_normalizer.py:137; src/mindroom/inbound_turn_normalizer.py:188; src/mindroom/coalescing.py:106; src/mindroom/turn_controller.py:241
PendingDispatchMetadata	class	lines 61-67	related-only	PendingDispatchMetadata requires_solo_batch close reservation metadata	src/mindroom/coalescing_batch.py:145; src/mindroom/turn_controller.py:122
DispatchIngressMetadata	class	lines 71-77	related-only	DispatchIngressMetadata source_kind hook_source message_received_depth	src/mindroom/conversation_resolver.py:133; src/mindroom/turn_controller.py:268
DispatchPayloadMetadata	class	lines 81-89	duplicate-found	DispatchPayloadMetadata attachment_ids original_sender raw_audio_fallback mentioned_user_ids formatted_bodies skip_mentions	src/mindroom/conversation_resolver.py:69; src/mindroom/coalescing_batch.py:87; src/mindroom/turn_controller.py:1696
DispatchHandoff	class	lines 93-105	related-only	DispatchHandoff batch handoff source_event_ids media_events dispatch_metadata	src/mindroom/coalescing_batch.py:40; src/mindroom/turn_controller.py:1446; src/mindroom/handled_turns.py:66
_event_content_dict	function	lines 108-114	duplicate-found	event source content dict isinstance cast	src/mindroom/coalescing_batch.py:62; src/mindroom/matrix/message_content.py:40; src/mindroom/matrix/visible_body.py:102
is_media_dispatch_event	function	lines 117-119	related-only	is_media_dispatch_event is_matrix_media_dispatch_event image file video	src/mindroom/matrix/media.py:69; src/mindroom/coalescing.py:280
dispatch_prompt_for_event	function	lines 122-133	related-only	extract_media_caption attached image video file raw audio prepared text	src/mindroom/matrix/media.py:152; src/mindroom/voice_handler.py:216; src/mindroom/coalescing_batch.py:166
_collect_batch_mentions_and_formatted_bodies	function	lines 136-163	duplicate-found	m.mentions user_ids formatted_body skip_mentions collect dedupe	src/mindroom/thread_utils.py:27; src/mindroom/conversation_resolver.py:44; src/mindroom/conversation_resolver.py:69
_batch_payload_metadata	function	lines 166-182	duplicate-found	batch payload metadata attachment_ids original_sender raw_audio_fallback sidecar preview	src/mindroom/coalescing_batch.py:87; src/mindroom/coalescing_batch.py:190; src/mindroom/turn_controller.py:1696
payload_metadata_from_source	function	lines 185-221	duplicate-found	payload metadata from source m.mentions formatted_body attachment_ids original_sender raw_audio_fallback skip_mentions	src/mindroom/conversation_resolver.py:69; src/mindroom/attachments.py:93; src/mindroom/turn_controller.py:1696
merge_payload_metadata	function	lines 224-258	related-only	merge payload metadata hydrated trust internal fill unknown defaults	src/mindroom/turn_controller.py:1625; src/mindroom/conversation_resolver.py:69
_merge_batch_source	function	lines 274-293	duplicate-found	merge batch source m.mentions formatted_body original_sender attachment_ids internal keys	src/mindroom/conversation_resolver.py:69; src/mindroom/matrix/large_messages.py:37; src/mindroom/voice_handler.py:221
_single_prepared_dispatch_event	function	lines 296-299	related-only	PreparedTextEvent replace source_kind_override message	src/mindroom/coalescing.py:106; src/mindroom/conversation_resolver.py:143
build_batch_dispatch_event	function	lines 302-316	related-only	build PreparedTextEvent batch synthetic primary event source_kind	src/mindroom/inbound_turn_normalizer.py:188; src/mindroom/coalescing_batch.py:173
build_dispatch_handoff	function	lines 319-339	related-only	build dispatch handoff from CoalescedBatch source_event_ids media_events metadata	src/mindroom/coalescing_batch.py:173; src/mindroom/turn_controller.py:1446
```

## Findings

### 1. Event content extraction is literally duplicated

`dispatch_handoff._event_content_dict` at `src/mindroom/dispatch_handoff.py:108` and `coalescing_batch._event_content_dict` at `src/mindroom/coalescing_batch.py:62` perform the same behavior: accept a dispatch event, reject non-dict `source`, read `source["content"]`, reject non-dict content, and return a typed dict.

Related but not identical helpers exist in `matrix/message_content.py:40` and `matrix/visible_body.py:102`, where the behavior normalizes arbitrary content objects into empty/string-keyed dicts instead of returning `None`.

Differences to preserve:

- The dispatch/coalescing helpers return `None` when content is absent or invalid.
- Matrix content helpers normalize invalid content to `{}` and sometimes filter non-string keys.

### 2. Dispatch payload content writing is repeated across handoff and conversation resolution

`dispatch_handoff._merge_batch_source` at `src/mindroom/dispatch_handoff.py:274` writes a synthetic Matrix content payload from `DispatchPayloadMetadata`: `m.mentions`, joined `formatted_body`, `format`, original sender, raw-audio fallback, and attachment IDs.

`conversation_resolver._source_with_payload_metadata` at `src/mindroom/conversation_resolver.py:69` overlays the same mention and formatted-body facts back onto a resolved event source and also applies skip-mentions metadata.

`turn_controller._dispatch_text_message` at `src/mindroom/turn_controller.py:1696` repeats the attachment/original-sender/raw-audio subset when preparing extra content for downstream message/routing operations.

Differences to preserve:

- `_merge_batch_source` deliberately removes synthetic internal keys before reconstructing batch content.
- `_source_with_payload_metadata` must be able to clear `formatted_body` when metadata explicitly says it is empty.
- `turn_controller` builds extra content, not a full event source, and defaults to parsing attachment IDs only when no payload metadata was supplied.

### 3. Mention extraction is duplicated with inconsistent fallback behavior

`dispatch_handoff._collect_batch_mentions_and_formatted_bodies` at `src/mindroom/dispatch_handoff.py:136` and `payload_metadata_from_source` at `src/mindroom/dispatch_handoff.py:185` both extract `m.mentions.user_ids` by checking for a dict and filtering string IDs.

`thread_utils._extract_mentioned_user_ids` at `src/mindroom/thread_utils.py:27` also reads `m.mentions.user_ids`, but it falls back to parsing Matrix pills from `formatted_body` when `m.mentions` is absent or empty.

Differences to preserve:

- Dispatch payload metadata currently preserves only explicit `m.mentions.user_ids`; it does not infer mentions from HTML pills.
- `thread_utils` returns a list and does not dedupe.
- `_collect_batch_mentions_and_formatted_bodies` dedupes across batched events while preserving first-seen order.

### 4. Batch payload metadata partly repeats coalescing-batch metadata extraction

`dispatch_handoff._batch_payload_metadata` at `src/mindroom/dispatch_handoff.py:166` packages attachment IDs, original sender, raw-audio fallback, mentions, formatted bodies, and skip-mentions into `DispatchPayloadMetadata`.

`coalescing_batch._batch_metadata` at `src/mindroom/coalescing_batch.py:87` extracts original sender and raw-audio fallback from trusted pending events, while `build_coalesced_batch` at `src/mindroom/coalescing_batch.py:190` separately merges attachment IDs from trusted pending events.

Differences to preserve:

- `CoalescedBatch` stores attachment/original-sender/raw-audio fields as concrete batch facts.
- `DispatchPayloadMetadata` uses `None` to mean unknown/untrusted and suppresses metadata for a single raw sidecar preview at `dispatch_handoff.py:167`.

## Proposed Generalization

A small helper module such as `src/mindroom/dispatch_payload.py` would be enough if production code were being edited.

Recommended helpers:

- `dispatch_event_content(event: DispatchEvent) -> dict[str, object] | None` to replace the two literal `_event_content_dict` copies.
- `extract_explicit_mentions(content: Mapping[str, object]) -> tuple[str, ...]` for explicit `m.mentions.user_ids` only.
- `apply_payload_metadata_to_content(content: dict[str, Any], payload: DispatchPayloadMetadata, *, clear_empty_formatted_body: bool = False) -> dict[str, Any]` for the repeated mention/formatted-body/skip-mentions overlay.
- `payload_extra_content(payload: DispatchPayloadMetadata) -> dict[str, Any]` for the attachment/original-sender/raw-audio subset used by routing and message extras.

No broad architecture change is recommended.
The duplication is real, but it sits near dispatch boundaries where preserving trust semantics matters.

## Risk/tests

Main behavior risks:

- Accidentally trusting internal attachment/original-sender/raw-audio metadata from untrusted senders.
- Changing sidecar-preview handling so preview content leaks stale attachment or original-sender metadata.
- Changing mention semantics by adding formatted-body Matrix pill fallback to dispatch payload metadata.
- Changing empty formatted-body handling during hydration.

Tests that would need attention for any future refactor:

- Coalesced batches with multiple `m.mentions.user_ids`, duplicate IDs, and formatted bodies.
- Single v2 sidecar preview events where payload metadata should remain unknown.
- Trusted vs untrusted hydrated source metadata in `payload_metadata_from_source` and `merge_payload_metadata`.
- Router relay or media dispatch paths that depend on `ATTACHMENT_IDS_KEY`, `ORIGINAL_SENDER_KEY`, and `VOICE_RAW_AUDIO_FALLBACK_KEY`.
