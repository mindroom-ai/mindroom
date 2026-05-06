## Summary

Top duplication candidate: `_parse_datetime` in `src/mindroom/approval_manager.py` duplicates the same ISO parsing behavior in `src/mindroom/approval_events.py`.
Related but not recommended for refactor: bounded `OrderedDict` eviction resembles the voice normalization cache, and several async waiter/shutdown patterns resemble approval transport/event-cache infrastructure but are approval-specific.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ToolApprovalTransportError	class	lines 65-70	related-only	ToolApprovalTransportError RuntimeError reason approval transport	src/mindroom/approval_transport.py:122; src/mindroom/tool_approval.py:22
ToolApprovalTransportError.__init__	method	lines 68-70	related-only	RuntimeError reason self.reason approval transport	src/mindroom/approval_transport.py:122; src/mindroom/config/approval.py:29
_BoundedCardEventIds	class	lines 73-92	related-only	OrderedDict popitem last=False bounded cache contains discard	src/mindroom/voice_handler.py:56-90; src/mindroom/matrix/cache/sqlite_event_cache.py:265; src/mindroom/matrix/cache/postgres_event_cache.py:364
_BoundedCardEventIds.__init__	method	lines 74-76	related-only	OrderedDict bounded cache max_size init	src/mindroom/voice_handler.py:56-58; src/mindroom/matrix/cache/sqlite_event_cache.py:260-266; src/mindroom/matrix/cache/postgres_event_cache.py:353-365
_BoundedCardEventIds.add	method	lines 78-83	related-only	OrderedDict add popitem last=False bounded cache	src/mindroom/voice_handler.py:82-90
_BoundedCardEventIds.discard	method	lines 85-86	related-only	OrderedDict discard pop none set discard	src/mindroom/approval_transport.py:324-325; src/mindroom/voice_handler.py:93-96
_BoundedCardEventIds.__contains__	method	lines 88-89	related-only	contains bounded ids OrderedDict membership	src/mindroom/tool_approval.py:274-283; src/mindroom/approval_manager.py:1060-1073
_BoundedCardEventIds.__len__	method	lines 91-92	none-found	len bounded ids OrderedDict cache	none
_utcnow	function	lines 95-96	related-only	datetime.now UTC helper now	src/mindroom/tool_approval.py:96-102; src/mindroom/thread_summary.py:414-419; src/mindroom/memory/auto_flush.py:87
_parse_datetime	function	lines 99-103	duplicate-found	datetime.fromisoformat tzinfo replace UTC parse_datetime	src/mindroom/approval_events.py:147-151; src/mindroom/thread_tags.py:107; src/mindroom/attachments.py:232
_compact_preview_text	function	lines 106-112	related-only	json.dumps ensure_ascii sort_keys fallback str preview	src/mindroom/tool_system/tool_calls.py:328; src/mindroom/api/openai_compat.py:1126-1132
_json_preview_length	function	lines 115-116	related-only	json.dumps ensure_ascii sort_keys len preview	src/mindroom/approval_manager.py:119-176
_truncate_event_argument_value	function	lines 119-122	related-only	sanitize_failure_text max_length json preview truncate	src/mindroom/tool_system/tool_calls.py:293-328; src/mindroom/tool_system/tool_calls.py:377-415
_contains_sanitizer_truncation	function	lines 125-149	none-found	sanitizer truncation marker __truncated__ recursive sanitized original	none
_build_event_arguments_preview	function	lines 152-176	related-only	sanitize_failure_value arguments preview arguments_truncated tool approval	src/mindroom/tool_system/tool_calls.py:377-415; src/mindroom/approval_events.py:58-80
ApprovalDecision	class	lines 180-186	related-only	ApprovalDecision resolved_at status reason resolved_by	src/mindroom/tool_approval.py:96-102; src/mindroom/matrix/sync_certification.py:44; src/mindroom/history/types.py:93
SentApprovalEvent	class	lines 190-193	related-only	SentApprovalEvent event_id delivered event dataclass	src/mindroom/approval_transport.py:191-201; src/mindroom/matrix/client_delivery.py:474-485
ApprovalActionResult	class	lines 197-204	related-only	ApprovalActionResult consumed resolved error_reason thread_id card_event_id	src/mindroom/tool_approval.py:286-308; src/mindroom/approval_inbound.py:95-112
_LiveApprovalWaiter	class	lines 208-213	related-only	Future waiter approval_id card_event_id live waiter	src/mindroom/approval_manager.py:502-735; src/mindroom/stop.py:149
_PostCancelCleanupTask	class	lines 217-220	related-only	cleanup_future owner_loop send_task post cancel	src/mindroom/approval_transport.py:315-324; src/mindroom/approval_manager.py:934-963
_ActiveApprovalSend	class	lines 224-227	related-only	done_future owner_loop send_task active sends	src/mindroom/approval_manager.py:502-563; src/mindroom/approval_manager.py:903-932
ApprovalManager	class	lines 230-1215	related-only	approval manager live waiters Matrix approval cards startup cleanup	src/mindroom/tool_approval.py:238-349; src/mindroom/approval_transport.py:70-418; src/mindroom/approval_events.py:15-155
ApprovalManager.__init__	method	lines 237-260	related-only	runtime_paths storage_root transport hooks event_cache live_lock pending_by_card_event	src/mindroom/approval_transport.py:70-80; src/mindroom/workers/runtime.py:24-27
ApprovalManager.request_approval	async_method	lines 262-334	related-only	request approval send bind wait timeout missing context requester	src/mindroom/tool_approval.py:238-271; src/mindroom/approval_transport.py:153-209
ApprovalManager.resolve_approval	async_method	lines 336-354	related-only	resolve approval pending card emit terminal edit	src/mindroom/tool_approval.py:286-308; src/mindroom/approval_transport.py:211-313
ApprovalManager.get_pending_approval	async_method	lines 356-365	related-only	get pending approval approval_id live card event	src/mindroom/tool_approval.py:292-299; src/mindroom/approval_events.py:36-88
ApprovalManager.discard_pending_on_startup	async_method	lines 367-397	related-only	startup discard cached approval cards latest trusted edit	src/mindroom/approval_transport.py:403-418; src/mindroom/approval_events.py:101-115
ApprovalManager.handle_card_response	async_method	lines 399-420	related-only	handle card response live waiter approval action	src/mindroom/tool_approval.py:286-308; src/mindroom/approval_inbound.py:66-112
ApprovalManager.handle_live_approval_id_response	async_method	lines 422-444	related-only	handle live approval_id response Matrix approval action	src/mindroom/tool_approval.py:292-299; src/mindroom/approval_inbound.py:34-60
ApprovalManager._handle_live_waiter_response	async_method	lines 446-475	related-only	approver_user_id sender_id live waiter response room check	src/mindroom/tool_approval.py:286-308; src/mindroom/approval_inbound.py:89-112
ApprovalManager.configure_transport	method	lines 477-496	related-only	configure transport hooks update existing runtime	src/mindroom/approval_transport.py:91-100; src/mindroom/runtime_support.py:205-248
ApprovalManager._current_shutdown_reason	method	lines 498-500	related-only	shutdown_reason live_lock getter	src/mindroom/approval_manager.py:884-901; src/mindroom/workers/runtime.py:271-274
ApprovalManager._send_and_bind_waiter	async_method	lines 502-563	related-only	ensure_future shield run_coroutine_threadsafe cancelled cleanup bind waiter	src/mindroom/approval_transport.py:102-130; src/mindroom/approval_transport.py:315-324
ApprovalManager._cleanup_cancelled_send_when_event_arrives	async_method	lines 565-595	related-only	cancelled send event arrives shield bind settle cancelled	src/mindroom/approval_transport.py:333-357
ApprovalManager._bind_live_waiter	method	lines 597-620	related-only	bind live waiter card event from content pending_by_card_event	src/mindroom/approval_events.py:36-88; src/mindroom/approval_manager.py:1090-1104
ApprovalManager._settle_bound_waiter_as_cancelled	async_method	lines 622-627	related-only	settle cancelled expired default cancelled reason	src/mindroom/stop.py:374-400
ApprovalManager._settle_bound_waiter_as_expired	async_method	lines 629-667	related-only	expire waiter emit resolution remember resolved cancelled	src/mindroom/approval_manager.py:684-693; src/mindroom/approval_manager.py:884-901
ApprovalManager._await_waiter	async_method	lines 669-682	related-only	wrap_future shield wait_for timeout expire waiter	src/mindroom/stop.py:149; src/mindroom/approval_transport.py:124-129
ApprovalManager._expire_waiter	async_method	lines 684-693	related-only	expire waiter claim live resolution terminal edit	src/mindroom/approval_manager.py:629-667; src/mindroom/approval_manager.py:884-901
ApprovalManager._resolve_live_response	async_method	lines 695-735	related-only	resolve live response claim cancelled truncated approval	src/mindroom/tool_approval.py:286-308; src/mindroom/approval_inbound.py:66-112
ApprovalManager._yield_to_queued_cancellation	async_method	lines 738-743	none-found	call_soon checkpoint queued cancellation yield	none
ApprovalManager._discard_matrix_only_card	async_method	lines 745-774	related-only	discard matrix only card claim cleanup emit expired	src/mindroom/approval_transport.py:403-418
ApprovalManager._settle_waiter_with_terminal_edit	async_method	lines 776-799	related-only	terminal edit complete waiter fail closed approved denied	src/mindroom/approval_transport.py:211-313
ApprovalManager._emit_resolution	async_method	lines 801-831	related-only	edit event resolved content warning failure	src/mindroom/approval_transport.py:211-313; src/mindroom/matrix/client_delivery.py:448-471
ApprovalManager._pending_approval_for_card	async_method	lines 833-844	related-only	PendingApproval.from_card_event latest_status pending live waiter	src/mindroom/approval_events.py:36-98
ApprovalManager._latest_edit	async_method	lines 846-855	related-only	event_cache get_latest_edit sender	src/mindroom/matrix/conversation_cache.py:233-252; src/mindroom/matrix/client_visible_messages.py:240-271
ApprovalManager._latest_trusted_edit	async_method	lines 857-865	related-only	terminal_edit_matches_card_sender latest edit trusted sender	src/mindroom/approval_events.py:111-115; src/mindroom/matrix/message_content.py:232-233
ApprovalManager._scan_cached_room_cards	async_method	lines 867-882	related-only	get_recent_room_events event_type approval original card filter	src/mindroom/matrix/cache/event_cache.py:56-66; src/mindroom/approval_events.py:101-108
ApprovalManager.shutdown	async_method	lines 884-901	related-only	shutdown expire waiters drain active sends cleanup tasks	src/mindroom/tool_approval.py:339-349; src/mindroom/workers/runtime.py:271-274
ApprovalManager._drain_active_approval_sends	async_method	lines 903-932	related-only	drain active sends wrap_future asyncio.wait timeout cancel send_task	src/mindroom/approval_manager.py:934-963; src/mindroom/approval_transport.py:315-324
ApprovalManager._drain_post_cancel_cleanup_tasks	async_method	lines 934-963	related-only	drain cleanup tasks wrap_future asyncio.wait timeout cancel send_task	src/mindroom/approval_manager.py:903-932; src/mindroom/approval_transport.py:315-324
ApprovalManager._discard_post_cancel_cleanup_task	method	lines 965-967	related-only	discard cleanup task done callback set discard	src/mindroom/approval_transport.py:324-331
ApprovalManager.uses_storage_root	method	lines 969-971	related-only	storage_root runtime identity reuse manager	src/mindroom/runtime_support.py:205-248
ApprovalManager.has_live_work	method	lines 973-979	related-only	has live work pending resolving active cleanup	src/mindroom/workers/runtime.py:245-266
ApprovalManager._live_waiter_for_card	method	lines 981-983	related-only	pending_by_card_event get lock live waiter	src/mindroom/approval_manager.py:1016-1021
ApprovalManager._live_card_event_id_for_approval	method	lines 985-990	related-only	approval_id find card_event_id pending waiter	src/mindroom/tool_approval.py:292-299
ApprovalManager._claim_live_resolution	method	lines 992-1003	related-only	claim live resolution resolving ids resolved ids future done	src/mindroom/approval_manager.py:1005-1014; src/mindroom/approval_manager.py:1053-1058
ApprovalManager._claim_matrix_cleanup	method	lines 1005-1014	related-only	claim matrix cleanup pending resolving resolved ids	src/mindroom/approval_manager.py:992-1003; src/mindroom/approval_manager.py:745-774
ApprovalManager._complete_waiter	method	lines 1016-1021	related-only	complete waiter lookup set future result	src/mindroom/approval_manager.py:1024-1030
ApprovalManager._complete_waiter_direct	method	lines 1024-1030	related-only	future done set_result InvalidStateError	src/mindroom/approval_manager.py:1016-1021; src/mindroom/approval_manager.py:502-563
ApprovalManager._remember_resolved_card_event_id	method	lines 1032-1034	related-only	remember resolved card event id bounded set	src/mindroom/approval_manager.py:1036-1050
ApprovalManager._remember_cancelled_card_event_id	method	lines 1036-1038	related-only	remember cancelled card event id bounded set	src/mindroom/approval_manager.py:1032-1050
ApprovalManager._forget_cancelled_card_event_id	method	lines 1040-1042	related-only	forget cancelled card event id discard	src/mindroom/approval_manager.py:1032-1050
ApprovalManager._resolved_card_event_ids_contains	method	lines 1044-1046	related-only	resolved card event ids contains lock	src/mindroom/approval_manager.py:1048-1050
ApprovalManager._cancelled_card_event_ids_contains	method	lines 1048-1050	related-only	cancelled card event ids contains lock	src/mindroom/approval_manager.py:1044-1046
ApprovalManager._claimed_resolution	method	lines 1053-1058	related-only	contextmanager claimed resolution discard resolving id	src/mindroom/approval_manager.py:992-1014
ApprovalManager.knows_in_memory_approval_card	method	lines 1060-1068	related-only	knows in memory approval card pending resolving resolved cancelled	src/mindroom/tool_approval.py:274-277; src/mindroom/approval_inbound.py:112-127
ApprovalManager.has_active_in_memory_approval_card	method	lines 1070-1073	related-only	active in memory approval card pending resolving	src/mindroom/tool_approval.py:280-283
ApprovalManager._wait_for_competing_terminal_decision	async_method	lines 1075-1078	related-only	shield wrap_future waiter future competing terminal	src/mindroom/approval_manager.py:669-682; src/mindroom/approval_transport.py:124-129
ApprovalManager._configured_approval_room_ids	method	lines 1080-1083	related-only	provider optional configured approval room ids	src/mindroom/approval_transport.py:256-262
ApprovalManager._transport_sender_id	method	lines 1085-1088	related-only	transport sender provider optional user id	src/mindroom/approval_transport.py:248-254
ApprovalManager._card_event_from_content	method	lines 1090-1104	related-only	Matrix event dict event_id sender type origin_server_ts content	src/mindroom/matrix/cache/event_normalization.py:20-49; src/mindroom/approval_events.py:36-64
ApprovalManager._pending_event_content	method	lines 1107-1140	related-only	approval event content msgtype tool_call_id approval_id arguments requested_at expires_at	src/mindroom/approval_events.py:36-88; src/mindroom/approval_transport.py:153-209
ApprovalManager._resolved_event_content	method	lines 1143-1179	related-only	resolved approval event content requested_at expires_at resolution_reason	src/mindroom/approval_events.py:88-98; src/mindroom/approval_transport.py:272-313
ApprovalManager._event_body	method	lines 1182-1189	none-found	Approved Denied Expired Approval required tool_name body	none
ApprovalManager._normalized_resolution_request	method	lines 1192-1201	related-only	deny approved truncated arguments approval response	src/mindroom/tool_approval.py:286-308
ApprovalManager._new_decision	method	lines 1204-1215	related-only	ApprovalDecision status reason resolved_by resolved_at datetime now	src/mindroom/tool_approval.py:96-102
_lookback_cutoff_ms	function	lines 1218-1219	related-only	time.time hours cutoff ms lookback	src/mindroom/matrix/stale_stream_cleanup.py:242; src/mindroom/coalescing.py:251-256
get_approval_store	function	lines 1222-1224	related-only	module manager getter global manager	src/mindroom/mcp/toolkit.py:16-27; src/mindroom/workers/runtime.py:24-27
initialize_approval_store	function	lines 1227-1261	related-only	initialize module manager storage root configure transport live work	src/mindroom/tool_approval.py:311-328; src/mindroom/workers/runtime.py:245-266
shutdown_approval_manager	async_function	lines 1264-1271	related-only	shutdown module manager global none clear	src/mindroom/tool_approval.py:339-349; src/mindroom/workers/runtime.py:271-274
```

## Findings

### 1. Duplicate ISO datetime parsing

`src/mindroom/approval_manager.py:99-103` and `src/mindroom/approval_events.py:147-151` both implement the same behavior: accept `str | None`, return `None` for missing values, parse with `datetime.fromisoformat`, and attach `UTC` when the parsed value is naive.

This is real duplication because both modules process the same approval card timestamps.
`approval_manager._resolved_event_content` uses the parser when rebuilding terminal approval content, while `approval_events.PendingApproval.from_card_event` uses the parser to derive `created_at_ms` and timeout.

Differences to preserve: none observed in the implementation.
Both functions have identical semantics.

### 2. Related bounded-cache eviction, but approval-specific identity tracking

`src/mindroom/approval_manager.py:73-92` uses `OrderedDict` as a bounded insertion-order set for terminal approval card ids.
`src/mindroom/voice_handler.py:56-90` uses an `OrderedDict` for bounded voice normalization cache eviction.

The shared behavior is "bounded in-memory retention with oldest-entry eviction", but the interfaces differ.
Approval needs set-like add/discard/contains operations; voice normalization needs key/value cache operations and LRU refresh via `move_to_end`.

No refactor is recommended for this file because the common part would be a tiny generic cache abstraction with only two currently different call shapes.

### 3. Related Matrix approval content and edit flows, but split by responsibility

`src/mindroom/approval_manager.py:1107-1179` builds pending/resolved `io.mindroom.tool_approval` content.
`src/mindroom/approval_transport.py:153-313` sends that content, adds thread relations, and wraps terminal edits with `build_matrix_edit_content`.
`src/mindroom/approval_events.py:36-98` parses the same content schema back into `PendingApproval`.

This is related schema handling rather than duplicate implementation.
The manager owns canonical approval payload shape, the transport owns Matrix delivery/envelope behavior, and `approval_events.py` owns parsing/validation.
The only concrete duplicate inside this cluster is the datetime parser described above.

## Proposed Generalization

Move the duplicated datetime parser to one approval-local helper and import it from both modules.
A minimal location would be `src/mindroom/approval_events.py` if keeping approval parsing utilities together, or a tiny `src/mindroom/approval_time.py` if avoiding cross-import direction concerns.

Suggested plan:

1. Add one shared helper with the exact current semantics.
2. Replace `approval_manager._parse_datetime` and `approval_events._parse_datetime` call sites with the shared helper.
3. Keep the helper private to approval code unless another module needs the exact same naive-as-UTC behavior.
4. Run focused approval manager/event tests, then full pytest.

No refactor is recommended for bounded caches or waiter/drain flows.

## Risk/tests

Risk is low for the datetime helper if the exact implementation is moved unchanged.
Tests should cover naive ISO strings, timezone-aware ISO strings, and `None` through both approval event parsing and resolved event content reconstruction.

The bounded cache and async lifecycle similarities should remain untouched unless future call sites create a third identical pattern.
