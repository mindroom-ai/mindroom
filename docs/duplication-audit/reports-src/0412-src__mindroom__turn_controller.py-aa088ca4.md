# Summary

Top duplication candidate: text-like ingress handling is repeated between normal text events and sidecar-backed file previews in `src/mindroom/turn_controller.py`.
Both paths build an ingress envelope, apply deep synthetic hook suppression, run interactive text selection handling, compute a coalescing thread, and enqueue the prepared text event.

Related but lower-confidence duplication: timed normalization markers are repeated for text, voice, and file-sidecar normalization in `src/mindroom/turn_controller.py`.
This is local orchestration duplication rather than cross-module duplication, so a small local helper would be enough if this file is edited later.

The remaining symbols are mostly dependency surfaces, lifecycle sequencing, policy decisions, or calls into existing collaborators.
No broad refactor is recommended.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_queued_notice_dispatch_metadata	function	lines 122-134	related-only	queued_notice PendingDispatchMetadata requires_solo_batch close metadata	src/mindroom/coalescing_batch.py:147; src/mindroom/response_lifecycle.py:85; src/mindroom/response_runner.py:553
_queued_notice_reservation_from_metadata	function	lines 137-146	related-only	queued_notice reservation dispatch_metadata metadata kind	src/mindroom/coalescing_batch.py:147; src/mindroom/dispatch_handoff.py:61; src/mindroom/response_lifecycle.py:85
_EditRegenerator	class	lines 149-159	not-a-behavior-symbol	EditRegenerator handle_message_edit protocol	src/mindroom/edit_regenerator.py:1; src/mindroom/bot.py:327
_EditRegenerator.handle_message_edit	async_method	lines 152-159	not-a-behavior-symbol	handle_message_edit edit_regenerator protocol	src/mindroom/edit_regenerator.py:1
_PrecheckedEvent	class	lines 163-167	not-a-behavior-symbol	PrecheckedEvent requester_user_id event dataclass	none
TurnControllerDeps	class	lines 175-193	not-a-behavior-symbol	TurnControllerDeps dependency dataclass collaborators	src/mindroom/bot.py:327
TurnController	class	lines 197-2024	duplicate-found	text ingress media ingress dispatch pipeline sidecar voice	src/mindroom/turn_controller.py:1486; src/mindroom/turn_controller.py:1970; src/mindroom/bot.py:1423
TurnController._client	method	lines 202-207	related-only	client none runtime client not ready	src/mindroom/inbound_turn_normalizer.py:130
TurnController._requester_user_id	method	lines 209-229	related-only	effective requester ORIGINAL_SENDER_KEY get_effective_sender_id_for_reply_permissions	src/mindroom/authorization.py:145; src/mindroom/matrix/stale_stream_cleanup.py:1065; src/mindroom/response_runner.py:229
TurnController._sender_is_trusted_for_ingress_metadata	method	lines 231-233	related-only	extract_agent_name trusted sender ingress metadata	src/mindroom/conversation_resolver.py:151; src/mindroom/thread_utils.py:57
TurnController._should_trust_internal_payload_metadata	method	lines 235-237	related-only	trust internal payload metadata sender trusted	src/mindroom/dispatch_handoff.py:213; src/mindroom/conversation_resolver.py:151
TurnController._is_trusted_internal_relay_event	method	lines 239-255	related-only	trusted internal relay ORIGINAL_SENDER source_kind	src/mindroom/conversation_resolver.py:143; src/mindroom/dispatch_source.py:15; src/mindroom/dispatch_handoff.py:212
TurnController._is_trusted_router_relay_event	method	lines 257-266	related-only	trusted router relay ROUTER_AGENT_NAME extract_agent_name	src/mindroom/hooks/context.py:97; src/mindroom/authorization.py:11
TurnController._should_use_trusted_router_relay_context	method	lines 268-285	related-only	trusted router relay context payload original_sender	src/mindroom/conversation_resolver.py:523; src/mindroom/dispatch_handoff.py:212
TurnController._precheck_event	method	lines 287-322	related-only	authorized sender can_reply_to_sender is_handled precheck	src/mindroom/bot.py:1423; src/mindroom/approval_inbound.py:82; src/mindroom/bot_room_lifecycle.py:199
TurnController._precheck_dispatch_event	method	lines 324-335	none-found	typed precheck dispatch wrapper PrecheckedEvent	none
TurnController._mark_source_events_responded	method	lines 337-339	related-only	record_turn HandledTurnState source events responded	src/mindroom/commands/handler.py:313; src/mindroom/turn_store.py:100
TurnController._has_newer_unresponded_in_thread	method	lines 341-387	none-found	newer unresponded thread same requester replay guard command voice	none
TurnController._should_skip_deep_synthetic_full_dispatch	method	lines 389-406	related-only	hook_ingress_policy allow_full_dispatch deep synthetic	src/mindroom/hooks/context.py:97; src/mindroom/turn_policy.py:589
TurnController._should_bypass_coalescing_for_active_thread_follow_up	method	lines 408-422	related-only	active thread follow up has_active_response_for_target automation agent	src/mindroom/turn_policy.py:589; src/mindroom/response_runner.py:553
TurnController._enqueue_active_thread_follow_up	async_method	lines 424-466	related-only	active thread follow up reserve_waiting_human_message queued notice cancel	src/mindroom/response_runner.py:553; src/mindroom/response_lifecycle.py:163
TurnController._enqueue_prepared_text_for_dispatch	async_method	lines 468-513	duplicate-found	build ingress target active follow-up enqueue_for_dispatch src/mindroom/turn_controller.py:515; src/mindroom/turn_controller.py:1970
TurnController._enqueue_media_for_dispatch	async_method	lines 515-560	duplicate-found	build ingress target active follow-up enqueue_for_dispatch src/mindroom/turn_controller.py:468
TurnController._should_skip_router_before_shared_ingress_work	async_method	lines 562-599	related-only	router skip shared ingress mentioned agents thread explicit targeting	src/mindroom/thread_utils.py:106; src/mindroom/conversation_resolver.py:523
TurnController._coalescing_key_for_event	async_method	lines 601-612	related-only	coalescing key room thread requester	src/mindroom/coalescing.py:177; src/mindroom/coalescing_batch.py:37
TurnController._append_live_event_with_timing	async_method	lines 614-627	related-only	append_live_event timing ingress_cache_append	src/mindroom/matrix/cache/thread_writes.py:820; src/mindroom/matrix/conversation_cache.py:940
TurnController._resolve_text_event_with_ingress_timing	async_method	lines 629-644	duplicate-found	resolve text event ingress_normalize timing attach timing	src/mindroom/turn_controller.py:1926; src/mindroom/turn_controller.py:1980; src/mindroom/inbound_turn_normalizer.py:137
TurnController._enqueue_for_dispatch	async_method	lines 646-717	related-only	coalescing gate enqueue PendingEvent timing trusted_internal_relay	src/mindroom/coalescing.py:440; src/mindroom/coalescing_batch.py:37
TurnController._maybe_send_visible_voice_echo	async_method	lines 719-750	related-only	visible voice echo record_visible_echo send_text	src/mindroom/voice_handler.py:221; src/mindroom/turn_store.py:121; src/mindroom/handled_turns.py:320
TurnController._prepare_dispatch	async_method	lines 752-858	related-only	extract dispatch context build target envelope hooks suppressed agent mentioned	src/mindroom/conversation_resolver.py:523; src/mindroom/conversation_resolver.py:177; src/mindroom/turn_policy.py:133
TurnController._execute_command	async_method	lines 860-924	related-only	CommandHandlerContext send_response build_message_target handle_command	src/mindroom/commands/handler.py:88; src/mindroom/bot.py:1641
TurnController._execute_command.<locals>.send_response	nested_async_function	lines 872-892	related-only	send_response build_message_target delivery_gateway send_text	src/mindroom/bot.py:1641; src/mindroom/bot_room_lifecycle.py:46
TurnController.handle_interactive_selection	async_method	lines 926-1009	related-only	interactive selection ack generate_response record handled	src/mindroom/bot.py:1472; src/mindroom/interactive.py:481
TurnController._execute_router_relay	async_method	lines 1011-1134	related-only	router relay suggest_agent register_routed_attachment send_text	src/mindroom/routing.py:1; src/mindroom/inbound_turn_normalizer.py:230; src/mindroom/custom_tools/subagents.py:274
TurnController._router_handled_turn_outcome	method	lines 1136-1151	related-only	router handled visible echo outcome is_handled	src/mindroom/turn_store.py:125; src/mindroom/handled_turns.py:539
TurnController._finalize_dispatch_failure	async_method	lines 1153-1174	related-only	user friendly error stream status completed send_text	src/mindroom/error_handling.py:1; src/mindroom/response_runner.py:329
TurnController._log_dispatch_latency	method	lines 1176-1199	related-only	dispatch latency ThreadHistoryResult diagnostics logging	src/mindroom/timing.py:191
TurnController._execute_response_action	async_method	lines 1201-1408	related-only	response action generate_response generate_team_response_helper enrichment queued notice	src/mindroom/response_runner.py:830; src/mindroom/response_runner.py:2081; src/mindroom/turn_policy.py:133
TurnController._execute_response_action.<locals>.prepare_request_after_lock	nested_async_function	lines 1281-1352	related-only	prepare_after_lock payload builder enrichment ResponseRequest	src/mindroom/response_runner.py:724; src/mindroom/turn_policy.py:133
TurnController.handle_coalesced_batch	async_method	lines 1410-1459	related-only	coalesced batch handoff retarget dispatch metadata reservation	src/mindroom/dispatch_handoff.py:105; src/mindroom/coalescing.py:177
TurnController._dispatch_handoff	async_method	lines 1461-1479	related-only	dispatch handoff metadata reservation text dispatch	src/mindroom/dispatch_handoff.py:88; src/mindroom/coalescing_batch.py:147
TurnController.handle_text_event	async_method	lines 1481-1484	related-only	turn_thread_cache_scope handle text event	src/mindroom/turn_controller.py:1838
TurnController._handle_message_inner	async_method	lines 1486-1587	duplicate-found	text event precheck append normalize envelope interactive enqueue	src/mindroom/turn_controller.py:1970; src/mindroom/turn_controller.py:1847
TurnController._dispatch_text_message	async_method	lines 1589-1836	related-only	dispatch text command route response payload attachments	src/mindroom/response_runner.py:2081; src/mindroom/turn_policy.py:253; src/mindroom/inbound_turn_normalizer.py:330
TurnController._dispatch_text_message.<locals>.build_payload	nested_async_function	lines 1786-1818	related-only	build dispatch payload attachments media thread ids	src/mindroom/inbound_turn_normalizer.py:257; src/mindroom/inbound_turn_normalizer.py:330
TurnController.handle_media_event	async_method	lines 1838-1845	related-only	turn_thread_cache_scope handle media event	src/mindroom/turn_controller.py:1481
TurnController._handle_media_message_inner	async_method	lines 1847-1881	related-only	media precheck append live event special media enqueue	src/mindroom/turn_controller.py:1486; src/mindroom/turn_controller.py:1970
TurnController._dispatch_special_media_as_text	async_method	lines 1883-1907	related-only	audio file sidecar dispatch as text type branch	src/mindroom/matrix/media.py:1
TurnController._on_audio_media_message	async_method	lines 1909-1968	duplicate-found	timed normalize voice build envelope enqueue prepared text	src/mindroom/turn_controller.py:629; src/mindroom/turn_controller.py:1970; src/mindroom/inbound_turn_normalizer.py:158
TurnController._dispatch_file_sidecar_text_preview	async_method	lines 1970-2024	duplicate-found	file sidecar prepared text envelope interactive enqueue	src/mindroom/turn_controller.py:1486; src/mindroom/turn_controller.py:629
```

# Findings

## 1. Text-like ingress handling is duplicated between normal text and file sidecar previews

`TurnController._handle_message_inner` normalizes a text event, builds an ingress envelope, applies `_should_skip_deep_synthetic_full_dispatch`, computes `coalescing_thread_id`, optionally handles `interactive.handle_text_response`, and calls `_enqueue_prepared_text_for_dispatch` at `src/mindroom/turn_controller.py:1548`, `src/mindroom/turn_controller.py:1552`, `src/mindroom/turn_controller.py:1557`, `src/mindroom/turn_controller.py:1562`, `src/mindroom/turn_controller.py:1563`, and `src/mindroom/turn_controller.py:1580`.

`TurnController._dispatch_file_sidecar_text_preview` repeats the same text-like post-normalization sequence after `prepare_file_sidecar_text_event` at `src/mindroom/turn_controller.py:1988`, `src/mindroom/turn_controller.py:1993`, `src/mindroom/turn_controller.py:1998`, `src/mindroom/turn_controller.py:1999`, and `src/mindroom/turn_controller.py:2015`.

The behavior is functionally the same once each path has a `PreparedTextEvent`: both paths treat the prepared event as a text ingress event that can be suppressed by hook policy, consumed by an interactive selection, or queued for normal dispatch.
Differences to preserve: normal text must handle edits before this shared sequence, and file sidecar returns `True` to indicate that the media event was consumed.

## 2. Text and media enqueue paths duplicate active-follow-up dispatch wrapping

`TurnController._enqueue_prepared_text_for_dispatch` builds a `MessageTarget`, checks `_should_bypass_coalescing_for_active_thread_follow_up`, and either delegates to `_enqueue_active_thread_follow_up` or `_enqueue_for_dispatch` at `src/mindroom/turn_controller.py:481`, `src/mindroom/turn_controller.py:487`, `src/mindroom/turn_controller.py:492`, and `src/mindroom/turn_controller.py:503`.

`TurnController._enqueue_media_for_dispatch` repeats the same control flow after deriving a media source kind and envelope at `src/mindroom/turn_controller.py:526`, `src/mindroom/turn_controller.py:532`, `src/mindroom/turn_controller.py:539`, `src/mindroom/turn_controller.py:544`, and `src/mindroom/turn_controller.py:554`.

The behavior is nearly identical: target construction, active-response bypass detection, follow-up reservation, and fallback coalescing enqueue.
Differences to preserve: text receives an already-built envelope and may pass `dispatch_policy_source_kind`, hook source, message depth, and trusted payload metadata; media derives its own source kind and envelope.

## 3. Timed normalization boundaries are repeated locally

`TurnController._resolve_text_event_with_ingress_timing` wraps `normalizer.resolve_text_event` with `ingress_normalize_start`, `ingress_normalize_ready`, and `attach_dispatch_pipeline_timing` at `src/mindroom/turn_controller.py:637`, `src/mindroom/turn_controller.py:638`, `src/mindroom/turn_controller.py:642`, and `src/mindroom/turn_controller.py:643`.

`TurnController._on_audio_media_message` repeats the marker pattern around `normalizer.prepare_voice_event` at `src/mindroom/turn_controller.py:1926`, `src/mindroom/turn_controller.py:1928`, `src/mindroom/turn_controller.py:1929`, `src/mindroom/turn_controller.py:1936`, and `src/mindroom/turn_controller.py:1940`.

`TurnController._dispatch_file_sidecar_text_preview` repeats it around `normalizer.prepare_file_sidecar_text_event` at `src/mindroom/turn_controller.py:1980`, `src/mindroom/turn_controller.py:1982`, `src/mindroom/turn_controller.py:1983`, `src/mindroom/turn_controller.py:1985`, and `src/mindroom/turn_controller.py:1987`.

This is duplicated instrumentation rather than duplicated domain logic.
Differences to preserve: voice can return `None` and should mark the source handled, while file sidecar asserts a prepared event after identifying the event as sidecar-backed.

## 4. Authorization/reply-permission prechecks are related to reaction handling but not a clean dedupe target

`TurnController._precheck_event` resolves the effective requester, skips self-originated non-hook dispatches, checks handled-turn state, checks authorization, and checks reply permission at `src/mindroom/turn_controller.py:297`, `src/mindroom/turn_controller.py:302`, `src/mindroom/turn_controller.py:305`, `src/mindroom/turn_controller.py:308`, and `src/mindroom/turn_controller.py:318`.

Reaction handling in `AgentBot` performs related authorization and reply-permission checks at `src/mindroom/bot.py:1423` and `src/mindroom/bot.py:1433`.
The intent overlaps, but the event shapes and side effects differ: turn prechecks use effective requester IDs, handled-turn tracking, and Matrix event source metadata; reaction handling uses the direct reacting sender and then branches into stop/config/interactive hooks.
No refactor is recommended from this audit alone.

# Proposed Generalization

1. If editing this file later, extract a private helper such as `_dispatch_prepared_text_ingress(...)` inside `TurnController`.
It would take `room`, `prepared_event`, `dispatch_event`, `requester_user_id`, `dispatch_timing`, optional `source_kind`, optional `thread_id`, and optional `trust_internal_payload_metadata`, then perform envelope creation, deep synthetic skip, interactive selection, and `_enqueue_prepared_text_for_dispatch`.

2. Consider a second small helper such as `_mark_ingress_normalization(...)` only if another timed normalization path is added.
Current duplication is small and local enough that immediate refactoring is optional.

3. Do not generalize `_precheck_event` with reaction authorization yet.
The shared surface would likely need parameters for handled-turn side effects and effective requester resolution, which would make it less clear than the current code.

# Risk/tests

The highest-risk area is preserving file-sidecar return semantics and normal text edit handling if the prepared-text ingress helper is extracted.
Tests should cover normal text interactive selection, file sidecar interactive selection, deep synthetic hook suppression for both paths, and active-thread follow-up enqueue behavior for text and media.

No production code was edited.
