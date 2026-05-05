## Summary

The strongest duplication candidate is the Matrix room-state access pattern in `src/mindroom/hooks/state.py`, which overlaps with direct state reads and writes in `src/mindroom/thread_tags.py`, `src/mindroom/scheduling.py`, `src/mindroom/custom_tools/matrix_room.py`, and `src/mindroom/commands/config_confirmation.py`.
These callers repeat the same nio response classification and state-event filtering, but most preserve domain-specific error behavior, payload shaping, or parsing, so only a narrow shared Matrix state adapter would be safe.
No duplicate implementation of the hook fallback chaining itself was found.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
build_hook_room_state_querier	function	lines 13-42	duplicate-found	room_get_state_event room_get_state RoomGetStateResponse state_key content	src/mindroom/thread_tags.py:610, src/mindroom/scheduling.py:476, src/mindroom/custom_tools/matrix_room.py:370
build_hook_room_state_querier.<locals>._query	nested_async_function	lines 26-40	duplicate-found	room_get_state_event RoomGetStateEventError room_get_state RoomGetStateError ev state_key content	src/mindroom/scheduling.py:490, src/mindroom/thread_tags.py:610, src/mindroom/custom_tools/matrix_room.py:370
build_hook_room_state_putter	function	lines 45-65	duplicate-found	room_put_state RoomPutStateResponse RoomPutStateError bool success	src/mindroom/thread_tags.py:486, src/mindroom/commands/config_confirmation.py:153, src/mindroom/scheduling.py:532
build_hook_room_state_putter.<locals>._put	nested_async_function	lines 56-63	duplicate-found	room_put_state state_key content isinstance RoomPutState	src/mindroom/thread_tags.py:486, src/mindroom/commands/config_confirmation.py:153, src/mindroom/commands/config_confirmation.py:191
chain_hook_room_state_queriers	function	lines 68-84	none-found	chain fallback primary room_state_querier result is not None	src/mindroom/hooks/context.py:167, src/mindroom/orchestrator.py:986, src/mindroom/tool_system/runtime_context.py:410
chain_hook_room_state_queriers.<locals>._query	nested_async_function	lines 78-82	none-found	await primary fallback query None	src/mindroom/hooks/context.py:110, src/mindroom/hooks/context.py:328, src/mindroom/hooks/context.py:636
chain_hook_room_state_putters	function	lines 87-102	none-found	chain fallback primary room_state_putter bool success	src/mindroom/hooks/context.py:176, src/mindroom/orchestrator.py:993, src/mindroom/tool_system/runtime_context.py:411
chain_hook_room_state_putters.<locals>._put	nested_async_function	lines 97-100	none-found	await primary fallback put True	src/mindroom/hooks/context.py:124, src/mindroom/hooks/context.py:361, src/mindroom/hooks/context.py:651
```

## Findings

1. Matrix room-state reads are implemented in several places with the same core behavior.
   `src/mindroom/hooks/state.py:26` chooses `client.room_get_state_event` when `state_key` is present and `client.room_get_state` otherwise, returning `None` for nio error responses and mapping all events of one type to `{state_key: content}`.
   `src/mindroom/thread_tags.py:610` fetches all room state, rejects non-`RoomGetStateResponse`, filters by `THREAD_TAGS_EVENT_TYPE`, and processes each event's `state_key` and `content`.
   `src/mindroom/scheduling.py:476` and `src/mindroom/scheduling.py:490` repeat the all-state and single-state read split for scheduled task state.
   `src/mindroom/custom_tools/matrix_room.py:370` repeats the optional `event_type`/single-state versus full-state branch and response classification for a user-facing tool payload.
   Differences to preserve: hook state returns `None` on Matrix errors, thread tags raises `ThreadTagsError`, scheduling logs and returns empty results or user strings, and matrix room tools return structured JSON payload strings.

2. Matrix room-state writes are repeated with slightly different success semantics.
   `src/mindroom/hooks/state.py:56` writes one state event with `client.room_put_state` and returns `False` only for `RoomPutStateError`.
   `src/mindroom/thread_tags.py:486` writes one thread-tag state event and treats only `RoomPutStateResponse` as success, raising `ThreadTagsError` otherwise.
   `src/mindroom/commands/config_confirmation.py:153` and `src/mindroom/commands/config_confirmation.py:191` write or clear pending config state and repeat `RoomPutStateResponse` success checks with logging.
   `src/mindroom/scheduling.py:532`, `src/mindroom/scheduling.py:1550`, and `src/mindroom/scheduling.py:1587` write scheduled-task state directly and often do not inspect the response.
   Differences to preserve: some callers need exceptions or logs, some currently ignore Matrix error responses, and the hook helper's boolean policy is intentionally looser than a strict `RoomPutStateResponse` check.

3. Hook chaining is already centralized.
   `src/mindroom/hooks/state.py:68` and `src/mindroom/hooks/state.py:87` are the only found implementations of primary/fallback chaining for room-state hook callables.
   `src/mindroom/hooks/context.py:167` and `src/mindroom/hooks/context.py:176` consume those helpers rather than duplicating the fallback behavior.
   The similarly shaped methods in `src/mindroom/hooks/context.py:328`, `src/mindroom/hooks/context.py:636`, and `src/mindroom/hooks/context.py:726` delegate through `_query_bound_room_state`, so they are related wrappers, not duplicate chaining logic.

## Proposed Generalization

A minimal refactor, if production edits are later requested, would be to add a small Matrix state access helper near the Matrix boundary, for example `src/mindroom/matrix/room_state.py`.
It could expose one low-level `query_room_state_map(client, room_id, event_type, state_key=None) -> dict[str, Any] | None` and one `put_room_state_event(client, room_id, event_type, state_key, content) -> bool`, matching the hook semantics.
Domain modules could opt in only where `None`/`False` semantics fit, while stricter callers could still wrap the helper to raise or emit user-facing errors.
No refactor is recommended for `chain_hook_room_state_queriers` or `chain_hook_room_state_putters`; they are already the shared abstraction.

## Risk/tests

The main risk is changing error semantics across callers that intentionally distinguish `RoomGetStateEventResponse`, `RoomGetStateResponse`, `RoomPutStateResponse`, Matrix error objects, and transport exceptions.
Tests would need to cover hook room-state query with and without `state_key`, Matrix error responses, full-state event filtering, put success and failure, and fallback chaining for both query and put helpers.
Any later consolidation touching thread tags or scheduling should also cover their domain-specific parse and user-facing error paths.
