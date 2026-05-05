# Duplication Audit: `src/mindroom/coalescing_batch.py`

## Summary

Top duplication candidate: `_event_content_dict` is duplicated literally in `src/mindroom/dispatch_handoff.py`.
Most other behavior in this module is coalescing-specific batch reduction, with related consumers in `dispatch_handoff.py`, `coalescing.py`, `conversation_resolver.py`, and `turn_controller.py` but no clear active duplicate implementation.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
PendingEvent	class	lines 26-37	related-only	PendingEvent pending_events source_kind dispatch_metadata batch dataclass	src/mindroom/coalescing.py:263, src/mindroom/coalescing.py:267, src/mindroom/dispatch_handoff.py:34, src/mindroom/dispatch_handoff.py:60
CoalescedBatch	class	lines 41-59	related-only	CoalescedBatch build_dispatch_handoff source_event_ids media_events dispatch_metadata	src/mindroom/dispatch_handoff.py:319, src/mindroom/turn_controller.py:1410, src/mindroom/coalescing.py:363
_event_content_dict	function	lines 62-68	duplicate-found	event.source content isinstance content dict _event_content_dict	src/mindroom/dispatch_handoff.py:108, src/mindroom/conversation_resolver.py:142, src/mindroom/turn_controller.py:245, src/mindroom/attachments.py:93
_pending_event_trusts_internal_payload	function	lines 71-72	related-only	trust_internal_payload_metadata pending_event trust hydrated internal metadata	src/mindroom/dispatch_handoff.py:37, src/mindroom/dispatch_handoff.py:159, src/mindroom/dispatch_handoff.py:332
coalesced_prompt	function	lines 75-84	related-only	coalesced_prompt quick succession Treat them as one turn rebuilt_prompt_parts	src/mindroom/edit_regenerator.py:238
_batch_metadata	function	lines 87-104	related-only	ORIGINAL_SENDER_KEY VOICE_RAW_AUDIO_FALLBACK_KEY raw_audio_fallback original_sender payload_metadata_from_source	src/mindroom/dispatch_handoff.py:185, src/mindroom/turn_controller.py:1696, src/mindroom/voice_handler.py:221
_batch_source_kind	function	lines 110-112	none-found	_SOURCE_KIND_PRIORITY min source_kind voice image media resolved_source_kinds	none
_batch_dispatch_policy_source_kind	function	lines 115-126	related-only	dispatch_policy_source_kind multiple dispatch policy source kinds envelope metadata	src/mindroom/turn_controller.py:137, src/mindroom/conversation_resolver.py:232, src/mindroom/dispatch_handoff.py:325
_batch_hook_source	function	lines 129-138	related-only	hook_source multiple hook sources envelope ingress metadata	src/mindroom/conversation_resolver.py:133, src/mindroom/dispatch_handoff.py:325, src/mindroom/hooks/ingress.py:26
_batch_message_received_depth	function	lines 141-142	related-only	message_received_depth max pending_event envelope hook depth	src/mindroom/conversation_resolver.py:133, src/mindroom/hooks/context.py:781, src/mindroom/hooks/ingress.py:45
_batch_dispatch_metadata	function	lines 145-156	related-only	PendingDispatchMetadata requires_solo_batch close Coalesced batch carried multiple	src/mindroom/turn_controller.py:122, src/mindroom/turn_controller.py:137, src/mindroom/dispatch_handoff.py:60
close_pending_event_metadata	function	lines 159-163	related-only	close pending_event dispatch_metadata claimed work cannot dispatch	src/mindroom/coalescing.py:657, src/mindroom/dispatch_handoff.py:60
_batch_source_event_prompts	function	lines 166-170	related-only	source_event_prompts dispatch_prompt_for_event source_event_ids prompt map	src/mindroom/edit_regenerator.py:224, src/mindroom/handled_turns.py:96, src/mindroom/turn_store.py:196
build_coalesced_batch	function	lines 173-207	related-only	build_coalesced_batch CoalescedBatch attachment_ids media_events source_event_ids dispatch handoff	src/mindroom/coalescing.py:356, src/mindroom/coalescing.py:620, src/mindroom/dispatch_handoff.py:319
```

## Findings

### 1. Duplicated Matrix event content extraction

`src/mindroom/coalescing_batch.py:62` and `src/mindroom/dispatch_handoff.py:108` both define `_event_content_dict(event)` with the same behavior: verify `event.source` is a dict, fetch `source["content"]`, verify that is a dict, and return it as `dict[str, object]`.
The functions are literal duplicates except for import style and their local module scope.

Related code repeats the same shape inline in narrower contexts, for example `src/mindroom/conversation_resolver.py:142`, `src/mindroom/turn_controller.py:245`, and `src/mindroom/attachments.py:93`.
Those inline call sites are related but not equivalent because some accept arbitrary event source dictionaries, some rely on typed Matrix text events, and `attachments.py` continues into attachment ID normalization.

Differences to preserve: the duplicated helper operates on `DispatchEvent`, not raw source dictionaries, and returns `dict[str, object] | None`.

## Proposed Generalization

Move the duplicate event-content helper to the existing typed dispatch boundary, or a tiny shared source helper if import direction matters.
Minimal option: expose `event_content_dict(event: DispatchEvent) -> dict[str, object] | None` from `dispatch_handoff.py` and import it in `coalescing_batch.py`, since `coalescing_batch.py` already imports dispatch types and helpers from that module.

Do not generalize the batch reducers yet.
`_batch_source_kind`, `_batch_dispatch_policy_source_kind`, `_batch_hook_source`, `_batch_message_received_depth`, `_batch_dispatch_metadata`, `_batch_source_event_prompts`, and `build_coalesced_batch` are cohesive coalescing reductions with consumers elsewhere, not duplicate implementations.

## Risk/tests

Behavior risk for extracting `_event_content_dict` is low but import cycles should be checked because `dispatch_handoff.py` type-checks `CoalescedBatch` and `coalescing_batch.py` imports dispatch helpers.
Relevant tests would be coalescing batch construction, dispatch handoff payload metadata, and any tests covering trusted internal metadata propagation.
No production code was edited for this audit.
