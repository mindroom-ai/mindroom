# Summary

Top duplication candidates for `src/mindroom/stop.py`:

1. Matrix reaction event construction and `client.room_send(..., message_type="m.reaction")` is repeated in `StopManager.add_stop_button`, config confirmation reactions, interactive question reactions, and Matrix conversation tool reactions.
2. Matrix redaction followed by optional conversation-cache/outbound-redaction notification is repeated in stop-button cleanup/removal, bot provisional-message redaction, and Matrix API redaction tooling.
3. Strong-reference background task tracking with `asyncio.create_task`, `add_done_callback`, and removal from a task collection is repeated in `StopManager`, `background_tasks.create_background_task`, and approval transport cache-write tracking, but the ownership/error-handling semantics differ enough that this is only a small related pattern.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_TrackedMessage	class	lines 31-39	none-found	TrackedMessage dataclass message_id target task reaction_event_id run_id cancel_requested	src/mindroom/response_runner.py:612; src/mindroom/response_attempt.py:24; src/mindroom/bot.py:1439
StopManager	class	lines 42-464	related-only	StopManager stop reaction cancellation tracking cleanup add_stop_button remove_stop_button	src/mindroom/bot.py:1437; src/mindroom/response_attempt.py:145; src/mindroom/response_runner.py:1023; src/mindroom/matrix/stale_stream_cleanup.py:1218
StopManager.__init__	method	lines 45-50	related-only	cleanup_tasks strong reference background tasks initialized add_done_callback	src/mindroom/background_tasks.py:42; src/mindroom/approval_transport.py:315; src/mindroom/coalescing.py:175
StopManager._log_target	method	lines 53-58	none-found	room_id thread_id resolved_thread_id logging target dict	src/mindroom/response_runner.py:612; src/mindroom/bot.py:1439
StopManager.set_current	method	lines 60-83	none-found	track message generation tracked_messages message_id task reaction_event_id run_id	src/mindroom/response_attempt.py:129; src/mindroom/response_runner.py:612
StopManager.update_run_id	method	lines 85-112	none-found	update tracked run id cancel_requested schedule cleanup run_id	src/mindroom/response_runner.py:1023; src/mindroom/response_runner.py:1549; src/mindroom/response_runner.py:1640
StopManager._discard_cleanup_task	method	lines 114-117	related-only	add_done_callback remove cleanup task strong reference discard task	src/mindroom/background_tasks.py:52; src/mindroom/approval_transport.py:321; src/mindroom/coalescing.py:175
StopManager._track_cleanup_task	method	lines 119-122	related-only	create_task add_done_callback keep strong reference cleanup task	src/mindroom/background_tasks.py:42; src/mindroom/approval_transport.py:317; src/mindroom/knowledge/refresh_scheduler.py:163
StopManager._get_active_tracked_message	method	lines 124-129	none-found	tracked message task.done active message lookup	src/mindroom/response_runner.py:612; src/mindroom/history/compaction.py:134; src/mindroom/orchestrator.py:1920
StopManager.get_tracked_target	method	lines 131-136	none-found	get tracked target for message reaction routing	src/mindroom/bot.py:1439; src/mindroom/response_runner.py:612
StopManager._probe_graceful_cancel	async_method	lines 138-177	none-found	acancel_run wait_for probe graceful cancel run cancellation manager_failed not_live	src/mindroom/streaming_delivery.py:544; src/mindroom/knowledge/refresh_runner.py:252; src/mindroom/mcp/manager.py:280
StopManager._graceful_run_cancel_cleanup	async_method	lines 179-230	none-found	best effort Agno run cleanup hard task cancel manager_failed not_live requested	src/mindroom/knowledge/refresh_runner.py:242; src/mindroom/history/compaction.py:115; src/mindroom/orchestration/runtime.py:126
StopManager._schedule_graceful_run_cancel	method	lines 232-234	related-only	asyncio.create_task cleanup strong reference schedule background task	src/mindroom/background_tasks.py:42; src/mindroom/approval_transport.py:317; src/mindroom/knowledge/manager.py:1391
StopManager.clear_message	method	lines 236-296	duplicate-found	clear tracking delayed remove reaction room_redact notify_outbound_redaction cleanup task	src/mindroom/bot.py:1770; src/mindroom/custom_tools/matrix_api.py:1183; src/mindroom/matrix/stale_stream_cleanup.py:1223
StopManager.clear_message.<locals>.delayed_clear	nested_async_function	lines 246-283	duplicate-found	delayed cleanup redact reaction notify outbound redaction remove tracked message	src/mindroom/bot.py:1770; src/mindroom/custom_tools/matrix_api.py:1206; src/mindroom/matrix/stale_stream_cleanup.py:1223
StopManager.handle_stop_reaction	async_method	lines 298-348	related-only	stop reaction task cancel request_task_cancel USER_STOP_CANCEL_MSG task.done	src/mindroom/cancellation.py:20; src/mindroom/orchestration/runtime.py:132; src/mindroom/history/compaction.py:1172
StopManager.add_stop_button	async_method	lines 350-420	duplicate-found	m.reaction m.annotation room_send ignore_unverified_devices notify_outbound_event	src/mindroom/commands/config_confirmation.py:318; src/mindroom/interactive.py:734; src/mindroom/custom_tools/matrix_conversation_operations.py:349
StopManager.remove_stop_button	async_method	lines 422-464	duplicate-found	remove stop button room_redact notify_outbound_redaction reaction_event_id	src/mindroom/bot.py:1770; src/mindroom/custom_tools/matrix_api.py:1183; src/mindroom/matrix/stale_stream_cleanup.py:1223
```

# Findings

## 1. Matrix reaction sending is duplicated

`StopManager.add_stop_button` builds an `m.reaction` event with an `m.annotation` relation and sends it through `client.room_send` with `ignore_unverified_devices_for_config(config)` at `src/mindroom/stop.py:370`.

The same Matrix reaction payload shape and send call appear in:

- `src/mindroom/commands/config_confirmation.py:318` and `src/mindroom/commands/config_confirmation.py:334` for confirm/cancel reactions.
- `src/mindroom/interactive.py:734` for interactive question option reactions.
- `src/mindroom/custom_tools/matrix_conversation_operations.py:349` for the matrix conversation `react` operation.

The behavior is functionally the same: construct `{"m.relates_to": {"rel_type": "m.annotation", "event_id": target_event_id, "key": emoji}}`, send it as `m.reaction`, pass Matrix encryption-device policy, and check for `nio.RoomSendResponse`.

Differences to preserve:

- Stop buttons need to persist the returned reaction event id and optionally notify the outbound cache with a full synthetic event source.
- Config confirmation sends two fixed reactions and logs separate warning messages.
- Interactive question reactions iterate arbitrary option emojis and only log failures.
- Matrix conversation tools return structured tool results rather than just logging.

## 2. Matrix redaction plus outbound cache notification is duplicated

`StopManager.clear_message` and `StopManager.remove_stop_button` both redact a known reaction event id and then call `notify_outbound_redaction(room_id, reaction_event_id)` when supplied at `src/mindroom/stop.py:258` and `src/mindroom/stop.py:441`.

Similar redaction flows appear in:

- `src/mindroom/bot.py:1770`, which redacts a provisional visible event and notifies the conversation cache.
- `src/mindroom/custom_tools/matrix_api.py:1183`, which redacts a Matrix event and conditionally notifies the conversation cache at `src/mindroom/custom_tools/matrix_api.py:1206`.
- `src/mindroom/matrix/stale_stream_cleanup.py:1223`, which redacts stale stop reactions after restart but intentionally does not notify the outbound cache.

The shared behavior is "redact one Matrix event, interpret Matrix redaction errors, and update local outbound cache state when this process authored the redaction."

Differences to preserve:

- Stop-button removal treats exceptions as non-fatal cleanup and clears `tracked.reaction_event_id` only after a successful redaction call.
- Bot provisional-message redaction returns `bool` and logs `RoomRedactError`.
- Matrix API redaction has rate-limit checks, audit logging, and structured tool result payloads.
- Stale stream cleanup redacts many candidate reactions and logs per-event failures without cache notification.

## 3. Strong-reference task tracking is a related pattern, not a clear refactor target

`StopManager._track_cleanup_task` keeps cleanup tasks alive by appending them to `self.cleanup_tasks` and removing them in a done callback at `src/mindroom/stop.py:119`.

Similar patterns exist in:

- `src/mindroom/background_tasks.py:42`, which creates global background tasks, tracks optional owners, consumes exceptions, and supports shutdown waiting.
- `src/mindroom/approval_transport.py:317`, which tracks cache-write tasks and consumes failures in `_finish_cache_write`.

This is related task lifecycle handling, but not a strong duplication candidate because each site has different lifetime ownership and error-consumption requirements.

# Proposed Generalization

Add a small Matrix reaction helper only if this area is touched for feature work:

- Location: `src/mindroom/matrix/reactions.py`.
- Shape: a pure `build_reaction_content(event_id: str, key: str) -> dict[str, object]` plus an async `send_reaction(...) -> str | None` that sends the reaction and returns the event id on `nio.RoomSendResponse`.
- Keep outbound-cache notification outside the helper, because only some callers have enough sender/cache context to publish synthetic outbound events.

For redaction, a minimal helper could live in `src/mindroom/matrix/redaction.py` or `src/mindroom/matrix/client_delivery.py`, but no immediate refactor is recommended from this audit alone.
The callers have materially different return types, audit behavior, and error semantics.

# Risk/tests

Reaction helper extraction would need tests covering:

- Stop button event id persistence and outbound event notification in `tests/test_stop_emoji_reuse.py`.
- Config confirmation reaction sends for both fixed emojis.
- Interactive question reaction failure logging.
- Matrix conversation tool `react` result payloads.

Redaction helper extraction would need tests covering:

- Stop button cleanup/removal clears `reaction_event_id` only after successful redaction.
- Conversation cache outbound redaction notification is called exactly once where required.
- `RoomRedactError` and raised-exception paths preserve current logging and return behavior per caller.

No production-code change is recommended as part of this report-only task.
