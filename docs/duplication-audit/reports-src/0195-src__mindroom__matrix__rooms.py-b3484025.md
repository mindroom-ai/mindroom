## Summary

Top duplication candidate: `leave_non_dm_rooms` repeats DM filtering that `BotRoomLifecycle.rooms_to_actually_leave` already performs before calling it.
Most other symbols in `src/mindroom/matrix/rooms.py` are either thin wrappers around `MatrixState` / Matrix admin helpers or the canonical implementations consumed elsewhere.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_set_room_avatar_if_available	async_function	lines 45-77	related-only	resolve_avatar_path check_and_set_avatar room avatar set avatar	src/mindroom/matrix/avatar.py:114, src/mindroom/matrix/avatar.py:143, src/mindroom/constants.py:407
_configure_managed_room_access	async_function	lines 80-151	related-only	get_target_join_rule ensure_room_join_rule ensure_room_directory_visibility matrix_room_access	src/mindroom/config/matrix.py:178, src/mindroom/matrix/client_room_admin.py:233, src/mindroom/matrix/client_room_admin.py:309
_room_key_to_name	function	lines 154-164	none-found	replace underscore title room key name	none
load_rooms	function	lines 167-174	related-only	MatrixState.load rooms matrix_state_for_runtime rooms	src/mindroom/matrix/state.py:47, src/mindroom/matrix/state.py:90, src/mindroom/matrix/state.py:107
_get_room_aliases	function	lines 177-179	related-only	get_room_aliases MatrixState room aliases	src/mindroom/matrix/state.py:98
get_room_id	function	lines 182-185	related-only	get_room room_id MatrixState room lookup	src/mindroom/matrix/state.py:90
_add_room	function	lines 188-198	related-only	add_room save MatrixState room state	src/mindroom/matrix/state.py:94, src/mindroom/matrix/state.py:57
_remove_room	function	lines 201-208	none-found	remove room state del state.rooms save	none
resolve_room_aliases	function	lines 211-226	related-only	resolve_room_aliases get_all_configured_rooms configured rooms room aliases	src/mindroom/bot.py:184, src/mindroom/thread_utils.py:223
get_room_alias_from_id	function	lines 229-244	none-found	reverse room alias from room id get_room_alias_from_id	src/mindroom/teams.py:1403
_ensure_room_exists	async_function	lines 247-392	related-only	room_resolve_alias create_room managed_room_alias_localpart ensure_room_has_topic	src/mindroom/hooks/matrix_admin.py:26, src/mindroom/matrix/client_room_admin.py:38, src/mindroom/topic_generator.py:142
ensure_all_rooms_exist	async_function	lines 395-446	none-found	ensure_all_rooms_exist get_all_configured_rooms get_agent_ids_for_room	src/mindroom/orchestrator.py:1472
_ensure_root_space_exists	async_function	lines 449-493	related-only	managed_space_alias_localpart room_resolve_alias create_space join_room space_room_id	src/mindroom/matrix/client_room_admin.py:145, src/mindroom/hooks/matrix_admin.py:26, src/mindroom/matrix/state.py:102
ensure_root_space	async_function	lines 496-533	related-only	add_room_to_space ensure_room_name root space avatar	src/mindroom/matrix/client_room_admin.py:322, src/mindroom/matrix/client_room_admin.py:351, src/mindroom/hooks/matrix_admin.py:58
ensure_user_in_rooms	async_function	lines 536-579	related-only	MatrixID.from_username matrix_client login join_room internal user rooms	src/mindroom/matrix/client_session.py:1, src/mindroom/matrix/identity.py:75, src/mindroom/matrix/client_room_admin.py:389
_dm_cache_key	function	lines 588-594	none-found	dm cache key user_id room_id tuple	none
_get_direct_room_ids	async_function	lines 597-622	none-found	list_direct_rooms DirectRoomsResponse M_NOT_FOUND m.direct cache	tests/test_dm_detection.py:58, tests/test_dm_detection.py:169
_is_two_member_group_room	function	lines 625-638	none-found	is_group member_count topic two member group room	none
_has_is_direct_marker	function	lines 641-651	none-found	m.room.member is_direct marker state events	tests/test_dm_detection.py:27
is_dm_room	async_function	lines 654-694	related-only	is_dm_room m.direct room_get_state is_direct cleanup turn_controller	src/mindroom/matrix/room_cleanup.py:99, src/mindroom/bot_room_lifecycle.py:147, src/mindroom/turn_controller.py:1724
leave_non_dm_rooms	async_function	lines 697-710	duplicate-found	leave_non_dm_rooms rooms_to_actually_leave is_dm_room leave_room	src/mindroom/bot_room_lifecycle.py:121, src/mindroom/bot_room_lifecycle.py:147, src/mindroom/matrix/client_room_admin.py:452
```

## Findings

### Duplicate DM filtering before room leave

`src/mindroom/matrix/rooms.py:697` loops through candidate room IDs, calls `is_dm_room`, preserves DMs, and leaves the rest.
`src/mindroom/bot_room_lifecycle.py:147` has `rooms_to_actually_leave`, which also filters `rooms_to_leave()` by `not await is_dm_room(client, room_id)`.
`src/mindroom/bot_room_lifecycle.py:121` then passes that already-filtered list to `leave_non_dm_rooms` when no explicit `room_ids` argument is supplied, so the default lifecycle path repeats the same DM checks.

The behavior is functionally duplicated because both locations implement the same policy decision: candidates that are DMs must not be left.
The difference to preserve is that `leave_non_dm_rooms` remains a defensive public helper used by `src/mindroom/bot.py:1211` with raw joined room IDs, while `rooms_to_actually_leave` is useful for callers/tests that need to inspect the exact leave set without performing the leave.

## Proposed Generalization

Minimal refactor if production edits are later allowed:

1. Add a small helper in `src/mindroom/matrix/rooms.py`, for example `filter_non_dm_rooms(client, room_ids) -> list[str]`, containing the `is_dm_room` loop.
2. Change `BotRoomLifecycle.rooms_to_actually_leave` to call that helper.
3. Change `leave_non_dm_rooms` to call that helper once and then only perform `leave_room` on the filtered result.
4. Keep `leave_non_dm_rooms` as the defensive API for callers that have not already filtered.
5. Add or adjust tests so the lifecycle default path does not call `is_dm_room` twice per leave candidate.

No refactor recommended for the remaining related-only symbols.
The room creation, root Space reconciliation, avatar, access-policy, and state-wrapper functions mostly compose already-centralized helpers rather than duplicating their internals.

## Risk/tests

Risk is low if the helper preserves ordering and calls `is_dm_room` once per candidate list evaluation.
Tests to adjust or add: room invite/lifecycle tests around `rooms_to_actually_leave`, `leave_unconfigured_rooms`, and direct calls to `leave_non_dm_rooms`; DM detection tests should remain unchanged because `is_dm_room` is the canonical detector.
