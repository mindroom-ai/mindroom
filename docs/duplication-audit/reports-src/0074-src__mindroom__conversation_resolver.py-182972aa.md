## Summary

Top duplication candidates for `src/mindroom/conversation_resolver.py`:

1. Dispatch payload metadata overlay is duplicated with coalesced batch source assembly in `src/mindroom/dispatch_handoff.py`.
2. Trusted ingress source metadata parsing is partly duplicated with source-kind parsing in `src/mindroom/dispatch_source.py` and relay detection in `src/mindroom/turn_controller.py`.
3. Mention extraction and context construction are duplicated inside two resolver context paths, but the duplication is local and intentionally preserves different thread-history behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
should_skip_mentions	function	lines 44-53	related-only	com.mindroom.skip_mentions m.new_content skip_mentions	src/mindroom/dispatch_handoff.py:159; src/mindroom/dispatch_handoff.py:220; src/mindroom/delivery_gateway.py:562; src/mindroom/custom_tools/matrix_conversation_operations.py:86; src/mindroom/thread_utils.py:49
_with_skip_mentions_metadata	function	lines 56-66	related-only	com.mindroom.skip_mentions m.new_content write skip mentions	src/mindroom/delivery_gateway.py:562; src/mindroom/delivery_gateway.py:931; src/mindroom/custom_tools/matrix_conversation_operations.py:86; src/mindroom/dispatch_handoff.py:261
_source_with_payload_metadata	function	lines 69-88	duplicate-found	DispatchPayloadMetadata m.mentions formatted_body skip_mentions source overlay	src/mindroom/dispatch_handoff.py:136; src/mindroom/dispatch_handoff.py:185; src/mindroom/dispatch_handoff.py:274
MessageContext	class	lines 92-102	related-only	message context mentioned_agents thread_history requires_full_thread_history	src/mindroom/turn_controller.py:578; src/mindroom/commands/handler.py:200; src/mindroom/thread_utils.py:60
ConversationResolverDeps	class	lines 106-114	not-a-behavior-symbol	dataclass deps runtime logger conversation_cache	none
ConversationResolver	class	lines 118-714	related-only	conversation resolver context envelope thread membership ingress metadata	src/mindroom/turn_controller.py:239; src/mindroom/matrix/conversation_cache.py:472; src/mindroom/matrix/thread_membership.py:419
ConversationResolver._client	method	lines 123-128	none-found	Matrix client not ready runtime.client none	src/mindroom/matrix/client_delivery.py:124; src/mindroom/tool_system/runtime_context.py:227
ConversationResolver._matrix_id	method	lines 130-131	not-a-behavior-symbol	matrix_id accessor deps	none
ConversationResolver._envelope_ingress_metadata	method	lines 133-175	duplicate-found	com.mindroom.source_kind com.mindroom.hook_source message_received_depth trusted source kind	src/mindroom/dispatch_source.py:56; src/mindroom/turn_controller.py:232; src/mindroom/turn_controller.py:248; src/mindroom/hooks/context.py:94; src/mindroom/hooks/sender.py:72
ConversationResolver.build_message_target	method	lines 177-204	related-only	MessageTarget.resolve thread_start_root_event_id get_entity_thread_mode	src/mindroom/response_runner.py:600; src/mindroom/turn_controller.py:481; src/mindroom/streaming.py:1050; src/mindroom/commands/handler.py:210
ConversationResolver.resolve_response_thread_root	method	lines 206-221	related-only	resolved_thread_id response_envelope build_message_target	src/mindroom/response_runner.py:1461; src/mindroom/turn_controller.py:945; src/mindroom/turn_store.py:357
ConversationResolver.build_message_envelope	method	lines 223-275	duplicate-found	MessageEnvelope source_event_id attachment_ids mentioned_agents source_kind	src/mindroom/response_runner.py:756; src/mindroom/turn_policy.py:146; src/mindroom/bot.py:1956
ConversationResolver.build_ingress_envelope	method	lines 277-322	duplicate-found	MessageEnvelope ingress envelope attachment_ids mentioned_agents empty	src/mindroom/response_runner.py:756; src/mindroom/turn_policy.py:146; src/mindroom/bot.py:1956
ConversationResolver.coalescing_thread_id	async_method	lines 324-358	related-only	coalescing_thread_id resolve_event_thread_id_best_effort room thread mode	src/mindroom/turn_controller.py:1493; src/mindroom/matrix/conversation_cache.py:240; src/mindroom/matrix/thread_membership.py:277
ConversationResolver._explicit_thread_id_for_event	async_method	lines 360-380	related-only	resolve_event_thread_id thread_membership_access	src/mindroom/matrix/conversation_cache.py:109; src/mindroom/matrix/thread_membership.py:243; src/mindroom/commands/handler.py:200
ConversationResolver.resolve_related_event_thread_id_best_effort	async_method	lines 382-394	related-only	resolve_related_event_thread_id_best_effort wrapper	src/mindroom/matrix/thread_bookkeeping.py:155; src/mindroom/matrix/thread_membership.py:243
ConversationResolver.thread_membership_access	method	lines 396-414	related-only	thread_messages_thread_membership_access lookup_thread_id fetch_event_info fetch_thread_messages	src/mindroom/matrix/thread_membership.py:419; src/mindroom/matrix/thread_membership.py:441; src/mindroom/matrix/thread_room_scan.py:112; src/mindroom/matrix/thread_bookkeeping.py:371
ConversationResolver._read_thread_messages	async_method	lines 416-432	related-only	get_thread_messages full_history dispatch_safe caller_label	src/mindroom/matrix/conversation_cache.py:826; src/mindroom/custom_tools/matrix_conversation_operations.py:580; src/mindroom/response_runner.py:695
ConversationResolver._event_info_for_event_id	async_method	lines 434-450	duplicate-found	conversation_cache.get_event RoomGetEventResponse RoomGetEventError EventInfo.from_event	src/mindroom/matrix/thread_membership.py:488; src/mindroom/matrix/thread_membership.py:506; src/mindroom/matrix/conversation_cache.py:561
ConversationResolver.derive_conversation_context	async_method	lines 452-469	related-only	derive_conversation_context _resolve_thread_context full_history	src/mindroom/commands/handler.py:200; src/mindroom/inbound_turn_normalizer.py:161
ConversationResolver._resolve_thread_context	async_method	lines 471-503	related-only	resolve thread context thread_id thread_messages is_full_history	src/mindroom/matrix/conversation_cache.py:240; src/mindroom/matrix/thread_membership.py:243; src/mindroom/matrix/thread_membership.py:419
ConversationResolver.extract_dispatch_context	async_method	lines 505-521	related-only	extract_dispatch_context full_history false dispatch_safe true	src/mindroom/turn_controller.py:913; src/mindroom/edit_regenerator.py:136
ConversationResolver.extract_trusted_router_relay_context	async_method	lines 523-571	duplicate-found	resolve_event_source_content check_agent_mentioned EventInfo.from_event thread_id_from_edit MessageContext	src/mindroom/conversation_resolver.py:592; src/mindroom/turn_controller.py:239; src/mindroom/turn_controller.py:257
ConversationResolver.extract_message_context	async_method	lines 573-590	related-only	extract_message_context full_history dispatch_safe false	src/mindroom/edit_regenerator.py:136; src/mindroom/turn_controller.py:913
ConversationResolver.extract_message_context_impl	async_method	lines 592-659	duplicate-found	resolve_event_source_content check_agent_mentioned EventInfo.from_event get_entity_thread_mode MessageContext	src/mindroom/conversation_resolver.py:523; src/mindroom/turn_controller.py:578; src/mindroom/thread_utils.py:60
ConversationResolver.hydrate_dispatch_context	async_method	lines 661-684	related-only	requires_full_thread_history dispatch_hydration extract_message_context_impl	src/mindroom/response_runner.py:695; src/mindroom/turn_controller.py:936
ConversationResolver.cached_room	method	lines 686-691	related-only	cached_room client.rooms MatrixRoom none	src/mindroom/matrix/client_delivery.py:124; src/mindroom/custom_tools/matrix_room.py:215; src/mindroom/custom_tools/matrix_room.py:285
ConversationResolver.turn_thread_cache_scope	async_method	lines 694-697	related-only	conversation_cache.turn_scope asynccontextmanager	src/mindroom/matrix/conversation_cache.py:472; src/mindroom/turn_controller.py:913
ConversationResolver.fetch_thread_history	async_method	lines 699-714	related-only	fetch_thread_history get_thread_messages full_history dispatch_safe	src/mindroom/response_runner.py:695; src/mindroom/edit_regenerator.py:101; src/mindroom/matrix/conversation_cache.py:566
```

## Findings

### 1. Dispatch payload metadata is converted to Matrix content in two places

`_source_with_payload_metadata()` overlays `DispatchPayloadMetadata` onto event content by writing `m.mentions`, joining `formatted_bodies` into `formatted_body` with `format = org.matrix.custom.html`, and applying skip-mentions metadata in `src/mindroom/conversation_resolver.py:69`.
`_merge_batch_source()` performs the same outbound content materialization for coalesced batches in `src/mindroom/dispatch_handoff.py:274`, after `_collect_batch_mentions_and_formatted_bodies()` and `_batch_payload_metadata()` produce the same metadata shape in `src/mindroom/dispatch_handoff.py:136` and `src/mindroom/dispatch_handoff.py:166`.

Why this is duplicated: both code paths convert `DispatchPayloadMetadata` into Matrix event content fields for the next dispatch/context pass.
The resolver handles an already-existing event source overlay and can remove `formatted_body` when metadata explicitly says there are no formatted bodies.
The handoff path builds a synthetic batch source and additionally strips internal keys and writes original sender, raw-audio fallback, and attachment IDs.

### 2. Trusted ingress metadata parsing repeats source-kind key handling

`ConversationResolver._envelope_ingress_metadata()` resolves a source kind from explicit arguments, `PreparedTextEvent.source_kind_override`, trusted `com.mindroom.source_kind` content, and media fallback in `src/mindroom/conversation_resolver.py:133`.
`src/mindroom/dispatch_source.py:56` has the same basic `com.mindroom.source_kind` extraction and trusted sender gate for voice classification.
`TurnController._is_trusted_internal_relay_event()` repeats the `PreparedTextEvent.source_kind_override` versus content-key precedence in `src/mindroom/turn_controller.py:239`.
Hook producers write the same `com.mindroom.source_kind`, `com.mindroom.hook_source`, and `HOOK_MESSAGE_RECEIVED_DEPTH_KEY` contract in `src/mindroom/hooks/sender.py:72` and `src/mindroom/hooks/context.py:94`.

Why this is duplicated: the same trusted internal content keys are parsed and emitted across ingress, dispatch-source classification, and relay detection.
Differences to preserve: `_envelope_ingress_metadata()` also defaults audio/image/message and reads hook source/depth, while `dispatch_source.is_voice_event()` is deliberately narrower.

### 3. MessageEnvelope construction has local duplication between full and lightweight ingress paths

`build_message_envelope()` and `build_ingress_envelope()` both call `_envelope_ingress_metadata()`, build or accept a `MessageTarget`, parse attachment IDs, select body and agent name defaults, and populate the same `MessageEnvelope` fields in `src/mindroom/conversation_resolver.py:223` and `src/mindroom/conversation_resolver.py:277`.
The meaningful difference is that the full envelope maps `context.mentioned_agents` into configured agent names, while the lightweight ingress envelope intentionally uses an empty tuple.

Why this is duplicated: these are two variants of one envelope-construction behavior.
The duplication is local and low risk, but it is active and could drift if `MessageEnvelope` fields change.

### 4. Event lookup response normalization duplicates an existing thread-membership helper

`ConversationResolver._event_info_for_event_id()` fetches from `conversation_cache.get_event()`, returns `None` for `M_NOT_FOUND`, raises for other lookup failures, and returns `EventInfo.from_event()` for successful `RoomGetEventResponse` in `src/mindroom/conversation_resolver.py:434`.
`src/mindroom/matrix/thread_membership.py:488` defines `_event_info_from_lookup_response()` and `fetch_event_info_from_conversation_cache()` in `src/mindroom/matrix/thread_membership.py:506` for the same behavior.

Why this is duplicated: both normalize a room-get-event style response into `EventInfo | None`.
Differences to preserve: the resolver error string says "related Matrix event", while the shared helper says "Matrix event"; callers likely do not depend on the exact text except tests.

### 5. Mention and thread context extraction is duplicated inside the resolver

`extract_trusted_router_relay_context()` and `extract_message_context_impl()` both resolve sidecar content, overlay payload metadata, apply `should_skip_mentions()`, call `check_agent_mentioned()`, log when the current agent is mentioned, parse `EventInfo`, honor room mode, and construct `MessageContext` in `src/mindroom/conversation_resolver.py:523` and `src/mindroom/conversation_resolver.py:592`.

Why this is duplicated: both paths need the same mention facts and base event metadata.
Differences to preserve: the router-relay path avoids cache-backed thread hydration and uses only `event_info.thread_id or event_info.thread_id_from_edit`, with `requires_full_thread_history=True` when a thread root exists.

## Proposed Generalization

1. Add a small content helper near `DispatchPayloadMetadata`, likely in `src/mindroom/dispatch_handoff.py` or a focused `dispatch_payload_content.py`, that applies mentions/formatted bodies/skip mentions to a content dict.
2. Move the skip-mentions key constant and read/write helpers to that same boundary or to a focused Matrix content metadata module, then use it from both dispatch handoff and resolver.
3. Add a narrow ingress metadata helper for trusted `source_kind`, `hook_source`, and `message_received_depth` content keys if future edits touch those paths; keep media fallback in the resolver.
4. Replace `_event_info_for_event_id()` with `fetch_event_info_from_conversation_cache()` if exact error text is not required.
5. Optionally factor resolver-local mention extraction into a private helper returning `(resolved_event_source, mentioned_agents, am_i_mentioned, has_non_agent_mentions, event_info)`; keep the two thread-resolution branches separate.

## Risk/tests

Behavior risks are mainly around trust boundaries.
The skip-mentions and source-kind keys must only be honored from trusted agent-authored internal events where existing code requires that.
Any refactor should include tests covering coalesced batch metadata, trusted router relay context, edit-event `m.new_content` skip mentions, hook dispatch depth/source propagation, and `M_NOT_FOUND` event lookup behavior.

No production code was edited for this audit.
