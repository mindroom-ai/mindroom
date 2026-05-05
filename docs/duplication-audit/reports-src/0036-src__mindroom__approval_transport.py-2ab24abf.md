Summary: ApprovalMatrixTransport duplicates some low-level Matrix delivery mechanics already centralized in `matrix/client_delivery.py`, especially encrypted-room gating, `room_send` response handling, and edit envelope delivery.
It also locally repeats a detached-task strong-reference/error-logging pattern that exists in `background_tasks.py` and `orchestration/runtime.py`.
Thread relation lookup is related to many outbound Matrix paths, but this module's custom approval event payload makes that overlap related rather than directly duplicate.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_ApprovalTransportBot	class	lines 39-53	related-only	Protocol latest_thread_event_id_if_needed agent_name running client event_cache	src/mindroom/bot.py:584; src/mindroom/runtime_protocols.py:60
_ApprovalTransportBot.latest_thread_event_id_if_needed	async_method	lines 45-53	related-only	latest_thread_event_id_if_needed caller_label approval_transport_thread_relation	src/mindroom/bot.py:584; src/mindroom/matrix/conversation_cache.py:901; src/mindroom/matrix/cache/thread_reads.py:180
_approval_startup_lookback_hours	function	lines 56-62	none-found	tool_approval timeout_days lookback_hours expire_orphaned startup	none
_approval_relation_agent_name	function	lines 65-67	none-found	content.get agent_name fallback approval relation	none
ApprovalMatrixTransport	class	lines 71-418	related-only	ApprovalMatrixTransport approval Matrix transport send edit notice cache	src/mindroom/orchestrator.py:278; src/mindroom/approval_manager.py:244; src/mindroom/matrix/client_delivery.py:137
ApprovalMatrixTransport.capture_runtime_loop	method	lines 81-89	none-found	capture_runtime_loop runtime loop already bound different event loop	none
ApprovalMatrixTransport.bind_approval_runtime	method	lines 91-100	related-only	initialize_approval_runtime sender editor event_cache approval_room_ids transport_sender	src/mindroom/approval_manager.py:483; src/mindroom/tool_approval.py:317
ApprovalMatrixTransport._run_on_runtime_loop	async_method	lines 102-129	related-only	run_coroutine_threadsafe wrap_future sync_tool_bridge runtime_loop	src/mindroom/approval_manager.py:526
ApprovalMatrixTransport._approval_thread_relation	async_method	lines 131-151	related-only	build_thread_relation latest_thread_event_id_if_needed caller_label	src/mindroom/delivery_gateway.py:544; src/mindroom/delivery_gateway.py:600; src/mindroom/delivery_gateway.py:1014; src/mindroom/custom_tools/matrix_conversation_operations.py:79; src/mindroom/scheduling.py:753
ApprovalMatrixTransport.send_approval_event	async_method	lines 153-162	none-found	send_approval_event run_on_runtime_loop send_approval_event_now	none
ApprovalMatrixTransport.send_approval_event_now	async_method	lines 164-209	duplicate-found	room_send RoomSendResponse can_send_to_encrypted_room ignore_unverified_devices custom event	src/mindroom/matrix/client_delivery.py:137; src/mindroom/matrix/client_delivery.py:59; src/mindroom/custom_tools/matrix_conversation_operations.py:347
ApprovalMatrixTransport.edit_approval_event	async_method	lines 211-224	none-found	edit_approval_event run_on_runtime_loop edit_approval_event_now	none
ApprovalMatrixTransport._bot_has_approval_room	method	lines 226-234	related-only	client.rooms room_id in cached rooms approval room	src/mindroom/matrix/client_delivery.py:106; src/mindroom/matrix/client_delivery.py:111
ApprovalMatrixTransport.transport_bot	method	lines 236-246	none-found	router transport bot running client room	none
ApprovalMatrixTransport.transport_sender_id	method	lines 248-254	related-only	transport_sender_id user_id router bot approval_manager	src/mindroom/approval_manager.py:1085
ApprovalMatrixTransport.configured_approval_room_ids	method	lines 256-262	related-only	configured_approval_room_ids client.rooms approval manager	src/mindroom/approval_manager.py:1080
ApprovalMatrixTransport._ignore_unverified_devices	method	lines 264-270	related-only	ignore_unverified_devices_for_config config_provider ToolApprovalTransportError	src/mindroom/matrix/client_delivery.py:218; src/mindroom/stop.py:380; src/mindroom/interactive.py:744; src/mindroom/commands/config_confirmation.py:316
ApprovalMatrixTransport.edit_approval_event_now	async_method	lines 272-313	duplicate-found	build_matrix_edit_content room_send RoomSendResponse can_send_to_encrypted_room edit_message_result	src/mindroom/matrix/client_delivery.py:443; src/mindroom/matrix/client_delivery.py:460; src/mindroom/matrix/client_delivery.py:137
ApprovalMatrixTransport.track_cache_write	method	lines 315-322	duplicate-found	create_task strong reference add_done_callback log failure background task	src/mindroom/background_tasks.py:23; src/mindroom/orchestration/runtime.py:294; src/mindroom/stop.py:121
ApprovalMatrixTransport._finish_cache_write	method	lines 324-331	duplicate-found	task discard result warning background task done callback	src/mindroom/background_tasks.py:42; src/mindroom/orchestration/runtime.py:275
ApprovalMatrixTransport.cache_approval_event_now	async_method	lines 333-357	duplicate-found	room_get_event normalize_nio_event_for_cache store_event lookup fill	src/mindroom/matrix/conversation_cache.py:297; src/mindroom/matrix/conversation_cache.py:335; src/mindroom/matrix/conversation_cache.py:536
ApprovalMatrixTransport.send_notice	async_method	lines 359-401	duplicate-found	build_message_content m.notice send_message_result room_send RoomSendResponse	src/mindroom/delivery_gateway.py:905; src/mindroom/delivery_gateway.py:1021; src/mindroom/thread_summary.py:409; src/mindroom/matrix/client_delivery.py:137
ApprovalMatrixTransport.handle_bot_ready	async_method	lines 403-418	none-found	expire_orphaned_approval_cards_on_startup startup_discard router ready lookback	none
```

Findings:

1. Approval event send/edit bypasses the shared Matrix delivery wrapper.
`ApprovalMatrixTransport.send_approval_event_now` and `edit_approval_event_now` perform the same delivery skeleton as `send_message_result` / `edit_message_result`: check encrypted-room sendability, call `client.room_send`, pass `ignore_unverified_devices`, test for `nio.RoomSendResponse`, log failure, and return an event outcome.
The shared path lives in `src/mindroom/matrix/client_delivery.py:137` and its lower-level `_send_prepared_room_message` at `src/mindroom/matrix/client_delivery.py:59`.
The approval transport cannot directly use `send_message_result` today because approval cards use custom event type `io.mindroom.tool_approval` and should not run normal large-message preparation or text-message assumptions.
The duplicated behavior is the transport envelope and response normalization, not approval content construction.

2. Approval edits duplicate Matrix edit delivery mechanics.
`ApprovalMatrixTransport.edit_approval_event_now` builds `build_matrix_edit_content` and sends it with `message_type="io.mindroom.tool_approval"`.
`src/mindroom/matrix/client_delivery.py:443` and `src/mindroom/matrix/client_delivery.py:460` wrap replacement content and deliver edits for regular messages.
The approval path must preserve custom event type and avoid adding text edit fields such as `"* {new_text}"`, but the encrypted-room guard, ignore-unverified policy, response classification, and failure logging are repeated.

3. Approval notice sending repeats regular Matrix notice delivery instead of using `send_message_result`.
`ApprovalMatrixTransport.send_notice` builds notice content with `build_message_content` and then repeats direct `room_send` handling.
Comparable notice sends in `src/mindroom/delivery_gateway.py:905`, `src/mindroom/delivery_gateway.py:1021`, and `src/mindroom/thread_summary.py:409` build content and pass through `send_message_result` in `src/mindroom/matrix/client_delivery.py:137`.
The difference to preserve is that approval notices intentionally reply to the approval card event and use router transport availability checks before sending.

4. Background cache-write task tracking duplicates detached task helpers.
`track_cache_write` and `_finish_cache_write` keep a strong reference to a created task, remove it on completion, and log exceptions.
That pattern exists in the global helper `src/mindroom/background_tasks.py:23` and the orchestration helper `src/mindroom/orchestration/runtime.py:294`.
Approval transport adds one local behavior worth preserving: the `_cache_write_tasks` set is instance-scoped, which may make shutdown or ownership easier than a global registry.

5. Outbound approval event cache fill repeats point-lookup normalization/storage.
`cache_approval_event_now` calls `room_get_event`, checks `RoomGetEventResponse`, normalizes via `normalize_nio_event_for_cache`, and stores in the event cache.
The same point-lookup fill behavior exists in `src/mindroom/matrix/conversation_cache.py:297`, especially the remote fetch and normalization at `src/mindroom/matrix/conversation_cache.py:335` and persistence at `src/mindroom/matrix/conversation_cache.py:536`.
The approval path is narrower because it already knows the freshly sent event id and wants best-effort cache warmup, but the fetch-normalize-store sequence is duplicated.

Related-only observations:

- `_approval_thread_relation` mirrors a widespread pattern: fetch latest thread event id, then build a thread relation.
Similar paths appear in `src/mindroom/delivery_gateway.py:544`, `src/mindroom/delivery_gateway.py:600`, `src/mindroom/custom_tools/matrix_conversation_operations.py:79`, `src/mindroom/scheduling.py:753`, and other send helpers.
This is related rather than a direct duplicate because standard messages route through `format_message_with_mentions` / `build_message_content`, while approval cards need only a raw `m.relates_to` payload inside a custom event.

- `_ignore_unverified_devices` wraps `ignore_unverified_devices_for_config`, which many Matrix senders use directly.
The only unique behavior is raising `ToolApprovalTransportError` when config is unavailable, so no standalone refactor is recommended for that method.

Proposed generalization:

Add a small custom-event delivery helper in `src/mindroom/matrix/client_delivery.py`, for example `send_room_event_result(client, room_id, message_type, content, *, config, operation) -> DeliveredMatrixEvent | None`.
It should reuse `can_send_to_encrypted_room`, `_send_prepared_room_message`, and standard response logging without applying text-message large-message preparation unless explicitly requested.
Then `ApprovalMatrixTransport.send_approval_event_now`, `edit_approval_event_now`, and `send_notice` can keep their approval-specific room/bot/content rules while sharing delivery behavior.

For cache warmup, consider a focused helper near the conversation cache such as `fetch_and_store_event_cache_fill(client, event_cache, room_id, event_id)`.
Do this only if another outbound custom-event path needs the same cache warmup; otherwise the current local code is acceptable.

For cache-write background tasks, either use `create_background_task(..., owner=self, name=...)` if global task ownership is acceptable, or leave the instance-local set in place.
No refactor is necessary unless approval transport gains shutdown draining or more background tasks.

Risk/tests:

- Custom event delivery must keep `message_type="io.mindroom.tool_approval"` for approval cards and must not force `m.room.message` fields into approval event content.
- Approval edits must preserve the exact `m.replace` envelope from `build_matrix_edit_content` and the optional thread relation inserted into replacement content.
- Notice delivery should continue using `"msgtype": "m.notice"` and reply to `approval_event_id`.
- Cache warmup failures are intentionally best-effort and warning-only; shared helpers must not make approval sends fail after Matrix accepted the event.
- Tests should cover successful approval send, failed approval send response, encrypted-room guard returning false, approval edit replacement content, notice send content, and cache write failure logging.
