# Summary

Top duplication candidates for `src/mindroom/response_lifecycle.py`:

1. Per-key `asyncio.Lock` creation with capped eviction is duplicated between `ResponseLifecycleCoordinator._response_lifecycle_lock` and the OpenAI-compatible completion lock registry.
2. Session lookup by `SessionType` is duplicated between `_session_exists` and `ConversationStateWriter.persist_response_event_id_in_session_run`.
3. Response lifecycle finalization is already centralized here; other modules mostly call into it rather than duplicating it.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_QueuedMessageState	class	lines 38-76	related-only	queued human message state, asyncio.Event wait/is_set, pending_human_messages	src/mindroom/ai_runtime.py:97, src/mindroom/matrix/cache/write_coordinator.py:112, src/mindroom/matrix/cache/write_coordinator.py:828
_QueuedMessageState.begin_response_turn	method	lines 45-48	none-found	begin_response_turn, active_response_turns, existing_turn	none
_QueuedMessageState.finish_response_turn	method	lines 50-53	none-found	finish_response_turn, active_response_turns decrement	none
_QueuedMessageState.add_waiting_human_message	method	lines 55-57	related-only	add_waiting_human_message, pending_human_messages increment, event set	src/mindroom/ai_runtime.py:145
_QueuedMessageState.consume_waiting_human_message	method	lines 59-64	none-found	consume_waiting_human_message, pending_human_messages decrement, event clear	none
_QueuedMessageState.has_pending_human_messages	method	lines 66-67	related-only	has_pending_human_messages, pending queued notice	src/mindroom/ai_runtime.py:145
_QueuedMessageState.has_active_response_turn	method	lines 69-70	related-only	has_active_response_turn, active response, locked response	src/mindroom/bot.py:703, src/mindroom/response_runner.py:549
_QueuedMessageState.wait	async_method	lines 72-73	related-only	queued signal wait, asyncio.Event wait	src/mindroom/ai_runtime.py:145, src/mindroom/matrix/cache/write_coordinator.py:814
_QueuedMessageState.is_set	method	lines 75-76	related-only	is_set queued signal, event is_set	src/mindroom/ai_runtime.py:145
_QueuedHumanNotice	class	lines 79-81	not-a-behavior-symbol	enum queued human notice none waiting	none
QueuedHumanNoticeReservation	class	lines 85-103	related-only	queued notice reservation consume cancel metadata	src/mindroom/turn_controller.py:122, src/mindroom/turn_controller.py:137, src/mindroom/turn_controller.py:442
QueuedHumanNoticeReservation._release_waiting_human_message	method	lines 91-95	none-found	release waiting human message active flag consume once	none
QueuedHumanNoticeReservation.consume	method	lines 97-99	none-found	reservation consume release waiting human message	none
QueuedHumanNoticeReservation.cancel	method	lines 101-103	none-found	reservation cancel release waiting human message	src/mindroom/turn_controller.py:460, src/mindroom/turn_controller.py:464
ResponseLifecycleCoordinator	class	lines 107-251	duplicate-found	per target lifecycle locks, lock dict capped eviction, queued signal	src/mindroom/api/openai_compat.py:114, src/mindroom/api/openai_compat.py:205
ResponseLifecycleCoordinator._thread_key	method	lines 114-115	related-only	target room_id resolved_thread_id tuple key	src/mindroom/message_target.py:87, src/mindroom/message_target.py:97
ResponseLifecycleCoordinator._has_active_response_for_thread_key	method	lines 117-122	related-only	active response for target lock locked queued signal	src/mindroom/turn_policy.py:582, src/mindroom/bot.py:1501
ResponseLifecycleCoordinator.has_active_response_for_target	method	lines 124-126	related-only	has_active_response_for_target wrapper	src/mindroom/bot.py:703, src/mindroom/response_runner.py:549, src/mindroom/turn_controller.py:422
ResponseLifecycleCoordinator._response_lifecycle_lock	method	lines 128-144	duplicate-found	asyncio lock dict get create max 100 evict unlocked	src/mindroom/api/openai_compat.py:205
ResponseLifecycleCoordinator._get_or_create_queued_signal	method	lines 146-154	related-only	get or create queued signal dict by thread key	src/mindroom/ai_runtime.py:113
ResponseLifecycleCoordinator._should_signal_queued_message	method	lines 157-161	related-only	source_kind automation filter queued human notice	src/mindroom/turn_policy.py:582, src/mindroom/dispatch_source.py:23
ResponseLifecycleCoordinator.reserve_waiting_human_message	method	lines 163-177	related-only	reserve waiting human message active response queued dispatch	src/mindroom/response_runner.py:553, src/mindroom/turn_controller.py:442
ResponseLifecycleCoordinator._begin_response_turn_notice	method	lines 179-195	none-found	begin response turn notice existing turn lock locked	none
ResponseLifecycleCoordinator._consume_queued_human_notice	method	lines 197-206	none-found	consume queued human notice enum none waiting	none
ResponseLifecycleCoordinator.run_locked_response	async_method	lines 208-251	related-only	run locked response acquire release lock timing context	src/mindroom/api/openai_compat.py:971, src/mindroom/response_runner.py:615
SessionStartedWatch	class	lines 255-266	not-a-behavior-symbol	session started watch dataclass fields	none
ResponseLifecycleDeps	class	lines 270-274	not-a-behavior-symbol	response lifecycle deps dataclass fields	none
_session_exists	function	lines 277-285	duplicate-found	get team session else get agent session by SessionType	src/mindroom/conversation_state_writer.py:101
response_outcome_label	function	lines 288-304	related-only	suppressed cancelled error delivery_kind visible_response_preserved no_visible_response	src/mindroom/timing.py:1, tests/test_multi_agent_bot.py:3766
ResponseLifecycle	class	lines 307-532	related-only	response lifecycle finalization session started post response effects	src/mindroom/response_runner.py:780, src/mindroom/response_runner.py:972, src/mindroom/response_runner.py:1105
ResponseLifecycle.__init__	method	lines 310-323	not-a-behavior-symbol	constructor stores deps response_kind timing envelope correlation_id	none
ResponseLifecycle._log_effects_failure_after_visible_delivery	method	lines 325-338	related-only	post response effects failed after visible delivery logger error	src/mindroom/post_response_effects.py:1, tests/test_cancelled_response_hook.py:377
ResponseLifecycle._session_started_watch_is_needed	method	lines 340-367	related-only	session started eligibility hook registry storage probe	src/mindroom/history/compaction.py:242, tests/test_ai_user_id.py:1002
ResponseLifecycle.setup_session_watch	method	lines 369-396	related-only	precompute session started watch response paths	src/mindroom/response_runner.py:972, src/mindroom/response_runner.py:1769, src/mindroom/response_runner.py:1919
ResponseLifecycle._maybe_emit_session_started	async_method	lines 398-427	related-only	build SessionHookContext emit EVENT_SESSION_STARTED	src/mindroom/history/compaction.py:242, src/mindroom/hooks/execution.py:128
ResponseLifecycle.emit_session_started	async_method	lines 429-442	related-only	emit session started catch ordinary failures log exception	src/mindroom/response_runner.py:1105, src/mindroom/response_runner.py:1806, src/mindroom/response_runner.py:1957
ResponseLifecycle.finalize	async_method	lines 444-498	related-only	emit after response cancelled response apply effects timing summary	src/mindroom/delivery_gateway.py:1, src/mindroom/post_response_effects.py:117, tests/test_streaming_finalize.py:877
ResponseLifecycle.apply_effects_safely	async_method	lines 500-532	related-only	apply post response effects preserve visible delivery exceptions	src/mindroom/post_response_effects.py:117, tests/test_cancelled_response_hook.py:377
```

# Findings

## 1. Per-key lock registry with capped unlocked eviction

`ResponseLifecycleCoordinator._response_lifecycle_lock` keeps a dict of `asyncio.Lock` objects keyed by `(room_id, resolved_thread_id)`, returns an existing lock when present, evicts unlocked entries once the registry reaches 100 entries, creates a new lock, and stores it.
`src/mindroom/api/openai_compat.py:205` implements the same lock-registry behavior for OpenAI-compatible completions keyed by `(storage_root, agent_name, session_id)`.

The behavior is functionally the same: lazy per-key lock allocation, retention while locked, and opportunistic pruning of idle locks at a fixed cap.
Differences to preserve: response lifecycle eviction also removes the matching queued-message signal at `src/mindroom/response_lifecycle.py:141`, while OpenAI completion locks only remove the lock entry.

## 2. Session lookup by `SessionType`

`_session_exists` selects `get_team_session` when `session_type is SessionType.TEAM`, otherwise `get_agent_session`, and returns whether a session exists.
`ConversationStateWriter.persist_response_event_id_in_session_run` repeats the same `SessionType` branch at `src/mindroom/conversation_state_writer.py:101` before mutating a run.

The behavior is nearly the same session retrieval decision, with different return shape.
Differences to preserve: `_session_exists` returns `bool`; the writer needs the loaded session object so it can update metadata and call `storage.upsert_session(session)`.

# Proposed Generalization

1. Add a small helper for capped lock registries only if more call sites appear, for example `get_or_create_capped_lock(lock_map, key, max_entries=100, on_evict=None)`.
2. If added, keep it local to a focused concurrency utility module and pass `on_evict` from response lifecycle to clear `_thread_queued_signals`.
3. Add a typed session retrieval helper such as `get_session_for_type(storage, session_id, session_type)` in `mindroom.agent_storage` or a storage-adjacent module.
4. Implement `_session_exists` as `get_session_for_type(...) is not None` and use the same helper in `ConversationStateWriter`.
5. No refactor is recommended for queued-human notice bookkeeping, session-started emission, or post-response effect finalization; those behaviors are already centralized in this module.

# Risk/tests

Lock-registry deduplication risk is moderate because eviction side effects differ.
Tests should cover lock identity reuse, capped eviction of unlocked locks, preservation of locked entries, and response lifecycle signal cleanup on lock eviction.
Relevant tests include `tests/test_queued_message_notify.py` and `tests/test_multi_agent_bot.py`.

Session retrieval deduplication risk is low if the helper returns the same Agno session object types.
Tests should cover agent and team session lookup paths, especially `tests/test_ai_user_id.py`, `tests/test_cancelled_response_hook.py`, and tests around `ConversationStateWriter.persist_response_event_id_in_session_run`.

No production code was edited for this audit.
