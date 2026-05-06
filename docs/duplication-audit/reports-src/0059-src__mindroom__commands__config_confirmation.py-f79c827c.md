## Summary

Top duplication candidates for `src/mindroom/commands/config_confirmation.py`:

1. Reaction-button sending duplicates the Matrix `m.reaction` annotation content shape and response handling used by interactive questions, stop buttons, and Matrix custom tools.
2. Matrix state persistence for keyed records duplicates the same `room_put_state`/empty-content tombstone and `room_get_state`/event-type filtering pattern used by scheduled tasks and thread tags.
3. Pending record serialization with ISO timestamps is related to scheduled task and tool approval card persistence, but field semantics differ enough that a shared domain model is not recommended.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_PendingConfigChange	class	lines 28-70	related-only	"PendingConfigChange pending dataclass created_at to_dict from_dict Matrix state"	src/mindroom/approval_events.py:15; src/mindroom/scheduling.py:144; src/mindroom/attachments.py:60
_PendingConfigChange.is_expired	method	lines 39-42	related-only	"is_expired expires_at timeout_seconds created_at datetime.now UTC"	src/mindroom/approval_events.py:139; src/mindroom/oauth/state.py:148; src/mindroom/api/sandbox_worker_prep.py:93
_PendingConfigChange.to_dict	method	lines 44-54	related-only	"to_dict isoformat created_at Matrix state content"	src/mindroom/approval_manager.py:1107; src/mindroom/scheduling.py:532; src/mindroom/attachments.py:89
_PendingConfigChange.from_dict	method	lines 57-70	related-only	"from_dict datetime.fromisoformat content parse pending state"	src/mindroom/approval_events.py:35; src/mindroom/scheduling.py:450; src/mindroom/attachments.py:610
register_pending_change	function	lines 77-111	related-only	"register pending dict event_id requester pending_changes active_questions tracked_messages"	src/mindroom/interactive.py:453; src/mindroom/stop.py:359; src/mindroom/coalescing_batch.py:26
get_pending_change	function	lines 114-124	related-only	"get pending by event_id reacts_to pending map"	src/mindroom/interactive.py:455; src/mindroom/stop.py:1439; src/mindroom/scheduling.py:508
_remove_pending_change	function	lines 127-137	related-only	"remove pending pop event_id cleanup active question tracked message"	src/mindroom/interactive.py:495; src/mindroom/stop.py:1446; src/mindroom/scheduling.py:443
store_pending_change_in_matrix	async_function	lines 140-175	duplicate-found	"room_put_state state_key event_type content RoomPutStateResponse Matrix state persist"	src/mindroom/thread_tags.py:486; src/mindroom/scheduling.py:532; src/mindroom/topic_generator.py:160; src/mindroom/matrix/avatar.py:164
_remove_pending_change_from_matrix	async_function	lines 178-213	duplicate-found	"room_put_state empty content remove state tombstone state_key"	src/mindroom/thread_tags.py:486; src/mindroom/scheduling.py:1550; src/mindroom/scheduling.py:1587
restore_pending_changes	async_function	lines 216-292	duplicate-found	"room_get_state RoomGetStateResponse filter event type state_key content restore expired remove"	src/mindroom/scheduling.py:450; src/mindroom/scheduling.py:476; src/mindroom/thread_tags.py:610; src/mindroom/scheduling.py:1560
_cleanup	function	lines 295-297	related-only	"cleanup clear pending dict active_questions shutdown"	src/mindroom/interactive.py:750; src/mindroom/scheduling.py:413; src/mindroom/stop.py:359
add_confirmation_reactions	async_function	lines 300-347	duplicate-found	"m.reaction m.annotation room_send event_id key ignore_unverified_devices"	src/mindroom/interactive.py:720; src/mindroom/stop.py:350; src/mindroom/custom_tools/matrix_conversation_operations.py:344
handle_confirmation_reaction	async_function	lines 350-424	related-only	"ReactionEvent event.key reacts_to requester remove pending send response approval interactive stop"	src/mindroom/interactive.py:433; src/mindroom/bot.py:1406; src/mindroom/tool_approval.py:70
```

## Findings

### 1. Reaction annotation sending is repeated

`add_confirmation_reactions()` builds two Matrix reaction events manually in `src/mindroom/commands/config_confirmation.py:316` and `src/mindroom/commands/config_confirmation.py:334`.
The same `room_send(message_type="m.reaction", content={"m.relates_to": {"rel_type": "m.annotation", "event_id": ..., "key": ...}})` structure appears in `src/mindroom/interactive.py:732`, `src/mindroom/stop.py:370`, and `src/mindroom/custom_tools/matrix_conversation_operations.py:344`.

The behavior is functionally the same: send an emoji annotation to a target Matrix event with `ignore_unverified_devices_for_config(config)` and log or handle non-`RoomSendResponse` results.
Differences to preserve:

- Config confirmations add exactly `✅` and `❌` and only warn on failure.
- Interactive questions iterate arbitrary option emoji and include emoji in the warning metadata.
- Stop buttons need the returned reaction event id and an optional outbound-event notification.

### 2. Keyed Matrix room-state write/delete is repeated

`store_pending_change_in_matrix()` persists a typed record with `room_put_state(..., event_type=_PENDING_CONFIG_EVENT_TYPE, state_key=event_id)` at `src/mindroom/commands/config_confirmation.py:154`.
`_remove_pending_change_from_matrix()` deletes by writing `{}` to the same state key at `src/mindroom/commands/config_confirmation.py:193`.

The same keyed state write/tombstone behavior appears in thread tags via `_put_thread_tag_state()` at `src/mindroom/thread_tags.py:486`, where `record is None` becomes `{}`.
Scheduled task cancellation also writes replacement state for a key at `src/mindroom/scheduling.py:1550` and `src/mindroom/scheduling.py:1587`, though it uses a cancelled record rather than an empty tombstone.

The behavior is functionally similar: write one event-type/state-key content payload and verify Matrix accepted it.
Differences to preserve:

- Config confirmation swallows Matrix errors after logging.
- Thread tags raises `ThreadTagsError` on non-`RoomPutStateResponse`.
- Scheduled tasks currently do not validate `room_put_state` responses in `_persist_scheduled_task_state()`.

### 3. Matrix room-state restore/list loops are repeated

`restore_pending_changes()` fetches all room state, checks `RoomGetStateResponse`, filters by `_PENDING_CONFIG_EVENT_TYPE`, validates non-empty content, parses typed records, skips expired records, and repopulates an in-memory registry at `src/mindroom/commands/config_confirmation.py:227`.

Similar event-type filtering over `room_get_state()` exists in scheduled tasks at `src/mindroom/scheduling.py:450` and `src/mindroom/scheduling.py:476`, and in thread tags at `src/mindroom/thread_tags.py:610`.
The behavior is functionally similar at the Matrix traversal layer, but the domain parse/merge logic differs.
Differences to preserve:

- Config confirmations remove expired entries from Matrix state and count restored vs expired records.
- Scheduled tasks filter by task status and parse `ScheduledWorkflow` payloads.
- Thread tags merge legacy state, per-tag records, and tombstones.

### 4. Pending record lifecycle is related but not a strong dedupe target

`_PendingConfigChange`, `register_pending_change()`, `get_pending_change()`, `_remove_pending_change()`, and `_cleanup()` form a small in-memory registry keyed by Matrix event id.
Interactive questions use a similar active-question registry at `src/mindroom/interactive.py:453` and removal at `src/mindroom/interactive.py:495`, while stop handling keeps tracked messages in `src/mindroom/stop.py:359`.

The operations are related, but the registries have different concurrency, persistence, and lifecycle rules.
Interactive uses locks and a persistence file; config confirmation uses module-level state plus Matrix room state; stop tracking lives on a manager instance and holds task objects.
No shared registry abstraction is recommended from this file alone.

### 5. Reaction handling is related to approval, stop, and interactive flows but remains domain-specific

`handle_confirmation_reaction()` checks requester ownership, ignores the bot user, accepts only `✅`/`❌`, removes pending state, applies or cancels the config change, then replies via `bot._send_response()` at `src/mindroom/commands/config_confirmation.py:365`.
Other reaction handlers in `src/mindroom/bot.py:1406`, `src/mindroom/interactive.py:433`, and tool approval routing have similar `event.key`/`event.reacts_to` dispatch mechanics.

The overlap is only the event-shape handling and reaction-key filtering.
The authorization, side effects, and response text are specific to config confirmation, so no broad handler abstraction is recommended.

## Proposed Generalization

1. Add a focused Matrix helper, likely in `src/mindroom/matrix/reactions.py`, for building/sending one annotation reaction: `send_annotation_reaction(client, room_id, target_event_id, emoji, *, config) -> nio.RoomSendResponse | nio.ErrorResponse`.
2. Update config confirmation and interactive reaction-button senders to use that helper first, because both only need warning-level failure handling.
3. Let stop-button handling optionally adopt the same helper only if the helper returns the raw response or event id without hiding failure details.
4. Consider a tiny keyed-state helper only if more call sites need consistent error behavior: `put_keyed_state(client, room_id, event_type, state_key, content)`.
5. Do not generalize pending registries or config-confirmation reaction handling unless another feature needs the same requester-bound confirm/cancel lifecycle.

## Risk/tests

Risks:

- Reaction helper changes can break encrypted-room delivery if `ignore_unverified_devices_for_config(config)` is not applied identically.
- Stop-button adoption must preserve returned reaction event ids and outbound cache notifications.
- Matrix state helper adoption must preserve each caller's current error policy: config confirmation logs and continues, thread tags raises, scheduled tasks mostly fire-and-forget.

Tests to cover if refactored:

- Existing config confirmation tests in `tests/test_config_commands.py` around registering, storing, and adding reactions.
- Reaction routing tests in `tests/test_edit_response_regeneration.py` for config confirmation authorization.
- Stop emoji reuse tests in `tests/test_stop_emoji_reuse.py` if stop button sending is changed.
- Scheduled restoration tests in `tests/test_scheduled_task_restoration.py` only if Matrix state helpers are touched.

Assumption: this task is audit-only, so no production code was changed.
