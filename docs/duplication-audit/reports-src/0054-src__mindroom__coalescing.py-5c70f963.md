Summary: `CoalescingGate` overlaps conceptually with `EventCacheWriteCoordinator`: both own keyed async queues, start one background task per lane, coalesce or gate pending work, clean up task maps, and expose drain/idle behavior.
The overlap is real at the lifecycle-pattern level, but the semantics differ enough that a shared queue abstraction is not recommended now.
A smaller related-only duplication exists around command detection/source-kind exclusions: `coalescing.is_command_event()` and `TurnController` both parse command bodies while excluding voice/synthetic cases.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
GatePhase	class	lines 62-67	related-only	GatePhase phase debounce grace in_flight lifecycle queue scheduler	src/mindroom/matrix/cache/write_coordinator.py:24, src/mindroom/matrix/cache/write_coordinator.py:47, src/mindroom/knowledge/registry.py:270
_QueueKind	class	lines 70-75	related-only	QueueKind normal command bypass queued update kind barrier	src/mindroom/matrix/cache/write_coordinator.py:32, src/mindroom/matrix/cache/write_coordinator.py:47, src/mindroom/matrix/cache/write_coordinator.py:269
_QueuedEvent	class	lines 79-81	related-only	QueuedEvent queued pending_event queue dataclass	src/mindroom/coalescing_batch.py:25, src/mindroom/matrix/cache/write_coordinator.py:32
_GateEntry	class	lines 85-93	related-only	GateEntry queue drain_task wake_event deadline state dataclass	src/mindroom/matrix/cache/write_coordinator.py:54, src/mindroom/knowledge/refresh_scheduler.py:38, src/mindroom/memory/auto_flush.py:56
_FlushDiagnostics	class	lines 97-103	related-only	FlushDiagnostics log_context timing_scope batch pending_count diagnostics	src/mindroom/matrix/cache/write_coordinator.py:386, src/mindroom/coalescing_batch.py:40
_effective_source_kind	function	lines 106-114	related-only	source_kind_override fallback source kind effective source	src/mindroom/dispatch_source.py:80, src/mindroom/conversation_resolver.py:146, src/mindroom/turn_controller.py:248
is_coalescing_exempt_source_kind	function	lines 117-122	related-only	exempt source kinds hook scheduled active_thread_follow_up trusted relay automation	src/mindroom/dispatch_source.py:17, src/mindroom/dispatch_source.py:51, src/mindroom/turn_policy.py:589
is_command_event	function	lines 125-137	related-only	command_parser parse voice image media source kind command event	src/mindroom/turn_controller.py:374, src/mindroom/turn_controller.py:573, src/mindroom/commands/parsing.py:97
_pending_has_only_text	function	lines 140-143	none-found	pending has only text RoomMessageText PreparedTextEvent media candidate	none
CoalescingGate	class	lines 146-764	related-only	keyed coalescing gate drain queue debounce grace dispatch EventCacheWriteCoordinator	src/mindroom/matrix/cache/write_coordinator.py:132, src/mindroom/knowledge/refresh_scheduler.py:38
CoalescingGate.__init__	method	lines 154-167	related-only	init dispatch_batch debounce_seconds upload_grace gates retired tasks	src/mindroom/matrix/cache/write_coordinator.py:136, src/mindroom/knowledge/refresh_scheduler.py:42
CoalescingGate.is_idle	method	lines 169-171	related-only	is_idle pending tasks queues active tasks	src/mindroom/matrix/cache/write_coordinator.py:608, src/mindroom/matrix/cache/write_coordinator.py:618
CoalescingGate._track_retired_in_flight_drain	method	lines 173-175	related-only	add_done_callback discard retired task tracking	src/mindroom/stop.py:121, src/mindroom/background_tasks.py:52, src/mindroom/knowledge/refresh_scheduler.py:163
CoalescingGate.retarget	method	lines 177-219	none-found	retarget re-key gate merge queues in-flight drain task	none
CoalescingGate._resolve_gate_entry	method	lines 221-233	none-found	resolve current key by gate identity after retarget	none
CoalescingGate._get_or_create_gate	method	lines 235-240	related-only	get or create keyed scheduler state setdefault	src/mindroom/matrix/cache/write_coordinator.py:188, src/mindroom/knowledge/refresh_scheduler.py:151
CoalescingGate._gate_work_count	method	lines 243-244	related-only	pending work count queue length pending chain length	src/mindroom/matrix/cache/write_coordinator.py:155, src/mindroom/matrix/cache/write_coordinator.py:496
CoalescingGate._oldest_pending_age_ms	method	lines 247-251	none-found	oldest pending age enqueue_time age ms	none
CoalescingGate._oldest_pending_events_age_ms	method	lines 254-256	none-found	oldest pending events age enqueue_time age ms	none
CoalescingGate._source_event_ids	method	lines 259-260	duplicate-found	source_event_ids pending_event event_id list	src/mindroom/coalescing_batch.py:197, src/mindroom/handled_turns.py:693
CoalescingGate._queue_pending_events	method	lines 263-264	none-found	queue pending events first count without pop	none
CoalescingGate._claim_front_events	method	lines 267-268	none-found	claim front events popleft count queue	none
CoalescingGate._front_normal_run_length	method	lines 271-277	none-found	front normal run length until non-normal barrier	none
CoalescingGate._extend_candidate_with_grace_media	method	lines 280-287	none-found	extend candidate with grace media is_media_dispatch_event	none
CoalescingGate._has_barrier_after_front_normal_run	method	lines 290-292	none-found	barrier after front normal run queue	none
CoalescingGate._has_item_after_candidate	method	lines 295-296	none-found	item after candidate count queue length	none
CoalescingGate._queue_kind	method	lines 299-306	related-only	queue kind bypass command normal coalescing exempt command event	src/mindroom/turn_controller.py:408, src/mindroom/turn_policy.py:589
CoalescingGate._enqueue_path	method	lines 308-315	none-found	enqueue path bypass command zero debounce schedule	none
CoalescingGate._log_enqueue	method	lines 317-336	related-only	log enqueue pending_count oldest_pending_age duration_ms	src/mindroom/matrix/cache/write_coordinator.py:551, src/mindroom/timing.py:191
CoalescingGate._log_enqueued_event	method	lines 338-354	none-found	coalescing_gate_message_enqueued event_id timing_scope	none
CoalescingGate._flush_diagnostics	method	lines 356-380	duplicate-found	flush diagnostics build batch source_event_ids log_context pending_count timing_scope	src/mindroom/coalescing_batch.py:173, src/mindroom/coalescing_batch.py:197
CoalescingGate._log_flush_finished	method	lines 383-398	related-only	log finished duration slow warning outcome	src/mindroom/matrix/cache/write_coordinator.py:551, src/mindroom/timing.py:191
CoalescingGate._ensure_drain_task	method	lines 400-406	related-only	ensure task create_task if none done named task	src/mindroom/bot.py:1256, src/mindroom/knowledge/refresh_scheduler.py:156, src/mindroom/matrix/cache/write_coordinator.py:566
CoalescingGate._schedule_drain	method	lines 408-410	related-only	schedule drain ensure task wake	src/mindroom/knowledge/refresh_scheduler.py:122, src/mindroom/matrix/cache/write_coordinator.py:465
CoalescingGate._wake	method	lines 413-415	related-only	wake event generation set waiters	src/mindroom/matrix/cache/write_coordinator.py:219, src/mindroom/memory/auto_flush.py:583
CoalescingGate._record_enqueue	method	lines 417-448	related-only	record enqueue logs emit elapsed timing	src/mindroom/matrix/cache/write_coordinator.py:386, src/mindroom/matrix/cache/write_coordinator.py:551
CoalescingGate.enqueue	async_method	lines 450-470	related-only	enqueue keyed work append schedule task coalesce pending	src/mindroom/matrix/cache/write_coordinator.py:465, src/mindroom/knowledge/refresh_scheduler.py:122
CoalescingGate.drain_all	async_method	lines 472-488	related-only	drain all active tasks await gather clear queues shutdown	src/mindroom/knowledge/refresh_scheduler.py:110, src/mindroom/bot.py:1289, src/mindroom/approval_manager.py:903
CoalescingGate._upload_grace_hard_cap_seconds	method	lines 490-495	none-found	upload grace hard cap multiplier max seconds	none
CoalescingGate._wait_for_deadline	async_method	lines 497-512	related-only	wait for deadline wake_event wait_for timeout generation	src/mindroom/memory/auto_flush.py:583, src/mindroom/api/main.py:177, src/mindroom/knowledge/watch.py:233
CoalescingGate._wait_for_debounce	async_method	lines 514-532	none-found	debounce wait reset deadline barrier after front normal run	none
CoalescingGate._wait_for_upload_grace	async_method	lines 534-574	none-found	upload grace late media hard cap extend candidate	none
CoalescingGate._log_dispatch_failure	method	lines 576-591	related-only	log dispatch failure pending count oldest age exception type	src/mindroom/bot.py:1284, src/mindroom/knowledge/refresh_scheduler.py:174
CoalescingGate._dispatch_events	async_method	lines 593-655	related-only	dispatch claimed batch in flight flush timing started finished cleanup	src/mindroom/matrix/cache/write_coordinator.py:500, src/mindroom/turn_controller.py:1410
CoalescingGate._dispatch_claimed_events	async_method	lines 657-673	related-only	dispatch wrapper close pending metadata on cancel or exception	src/mindroom/coalescing_batch.py:159, src/mindroom/turn_controller.py:424
CoalescingGate._drain_gate	async_method	lines 675-764	related-only	drain gate loop queue barrier debounce dispatch cleanup reschedule	src/mindroom/matrix/cache/write_coordinator.py:269, src/mindroom/matrix/cache/write_coordinator.py:448, src/mindroom/knowledge/refresh_scheduler.py:165
```

## Findings

1. Keyed async work coalescing/gating is duplicated as a lifecycle pattern, but not as a drop-in behavior.
`CoalescingGate` in `src/mindroom/coalescing.py:146` owns a `dict[CoalescingKey, _GateEntry]`, appends queued work, starts one drain task, wakes it through `asyncio.Event`, batches pending events after debounce/grace, and cleans up the key when the queue is empty.
`_EventCacheWriteCoordinator` in `src/mindroom/matrix/cache/write_coordinator.py:132` owns keyed room/thread scheduler state, queues `_QueuedUpdate` entries, coalesces replaceable pending updates by `coalesce_key`, starts updates once barriers permit, and cleans up room state in `_finish_entry()` at `src/mindroom/matrix/cache/write_coordinator.py:448`.
The shared behavior is "keyed async queue with pending work, task ownership, coalescing, idle/drain cleanup, and timing logs."
The differences are important: `CoalescingGate` batches inbound Matrix events after time windows and preserves command/bypass barriers, while the cache coordinator serializes advisory cache writes by room/thread barriers and replaces pending update factories instead of dispatching all queued events.

2. Command detection/source-kind filtering is repeated in related ingress paths.
`is_command_event()` in `src/mindroom/coalescing.py:125` accepts `RoomMessageText` or `PreparedTextEvent`, excludes voice and image/media source kinds, then calls `command_parser.parse(event.body)`.
`TurnController._should_skip_due_to_newer_unresponded_message()` repeats a narrower variant at `src/mindroom/turn_controller.py:374`, excluding voice before `command_parser.parse(message.body.strip())`.
`TurnController._should_skip_router_before_shared_ingress_work()` also parses commands directly at `src/mindroom/turn_controller.py:573`.
These paths all answer "does this text represent a command that should interrupt normal message handling," but the candidate object types and source-kind trust rules differ.

3. Source event id extraction is a tiny literal duplicate.
`CoalescingGate._source_event_ids()` in `src/mindroom/coalescing.py:259` returns `[pending_event.event.event_id for pending_event in pending_events]`.
`build_coalesced_batch()` repeats the same comprehension at `src/mindroom/coalescing_batch.py:197`.
This is real duplication, but it is too small to justify a helper on its own unless `coalescing_batch` becomes the source of flush diagnostics too.

## Proposed Generalization

No broad refactor recommended.
The keyed async queue similarity with `EventCacheWriteCoordinator` is active and repeated, but a shared abstraction would need to parameterize time windows, barrier semantics, retargeting, replacement-vs-batching, task ownership, and failure cleanup.
That would increase risk and indirection more than it reduces duplication.

A minimal future cleanup, if this file is touched for related work, would be:

1. Move command event classification into a small helper near `commands.parsing` or `dispatch_source`, preserving the current voice and media exclusions.
2. Use that helper from `coalescing.is_command_event()` and the two `TurnController` command checks only after tests pin the subtle source-kind differences.
3. Consider moving flush source-event-id/log-context construction into `coalescing_batch` only if another caller needs the same diagnostics.

## Risk/tests

The highest risk is accidentally changing command interruption behavior for voice, image/media, prepared text, and trusted internal relay messages.
Tests should cover `is_command_event()` for plain text commands, voice commands, media/image source kinds, and `PreparedTextEvent.source_kind_override`.
If command classification is centralized, add or update `TurnController` tests for newer-message skipping and router shared-ingress skipping.
For the queue lifecycle, no refactor is recommended; if attempted later, it would need async tests for debounce reset, upload grace hard cap, command/bypass barriers, retargeting while in-flight, `drain_all()`, dispatch failure cleanup, and idle reporting.
