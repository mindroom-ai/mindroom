Summary: The top duplication candidate is the Matrix state read/compare/write flow repeated by `ensure_thread_tags_power_level`, `ensure_room_join_rule`, `ensure_room_name`, `add_room_to_space`, and separate state writers in topic/avatar/config/scheduling/thread-tag modules.
No meaningful duplication was found for room creation, invitations, joining/leaving, directory visibility, or the room-name fallback logic beyond intentional use of the central helpers in `client_room_admin.py`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
invite_to_room	async_function	lines 24-35	none-found	room_invite invite_to_room invite_user	src/mindroom/hooks/matrix_admin.py:50; src/mindroom/orchestrator.py:1505; src/mindroom/matrix/client_room_admin.py:30
create_room	async_function	lines 38-77	related-only	room_create create_room initial_state power_users	src/mindroom/hooks/matrix_admin.py:33; src/mindroom/matrix/rooms.py:348; src/mindroom/matrix/client_room_admin.py:67
_with_thread_tags_power_level	function	lines 80-87	related-only	THREAD_TAGS_EVENT_TYPE power_levels events state_default	src/mindroom/thread_tags.py:509; src/mindroom/thread_tags.py:677; src/mindroom/matrix/client_room_admin.py:80
ensure_thread_tags_power_level	async_function	lines 90-142	duplicate-found	room_get_state_event room_put_state m.room.power_levels THREAD_TAGS_EVENT_TYPE	src/mindroom/thread_tags.py:685; src/mindroom/thread_tags.py:486; src/mindroom/matrix/client_room_admin.py:95
create_space	async_function	lines 145-168	related-only	room_create space private_chat create_space	src/mindroom/matrix/rooms.py:506; src/mindroom/matrix/client_room_admin.py:145; src/mindroom/matrix/client_room_admin.py:38
_describe_matrix_response_error	function	lines 171-180	related-only	ErrorResponse status_code message str(response)	src/mindroom/custom_tools/matrix_api.py:923; src/mindroom/custom_tools/matrix_room.py:277; src/mindroom/matrix/client_room_admin.py:171
_get_room_join_rule	async_function	lines 183-202	duplicate-found	room_get_state_event m.room.join_rules join_rule	src/mindroom/custom_tools/matrix_api.py:119; src/mindroom/custom_tools/matrix_room.py:370; src/mindroom/hooks/state.py:31
_set_room_join_rule	async_function	lines 205-230	duplicate-found	room_put_state m.room.join_rules join_rule	src/mindroom/custom_tools/matrix_api.py:1048; src/mindroom/hooks/state.py:56; src/mindroom/matrix/client_room_admin.py:211
ensure_room_join_rule	async_function	lines 233-243	duplicate-found	ensure room join_rule get then set if different	src/mindroom/matrix/client_room_admin.py:233; src/mindroom/matrix/rooms.py:126; src/mindroom/topic_generator.py:142
_get_room_directory_visibility	async_function	lines 246-257	none-found	room_get_visibility RoomGetVisibilityResponse visibility	src/mindroom/matrix/client_room_admin.py:246; none
_set_room_directory_visibility	async_function	lines 260-306	none-found	room_get_visibility Api.room_get_visibility client.send PUT visibility	src/mindroom/matrix/client_room_admin.py:260; none
ensure_room_directory_visibility	async_function	lines 309-319	related-only	ensure directory_visibility get then set if different	src/mindroom/matrix/rooms.py:127; src/mindroom/matrix/client_room_admin.py:309
ensure_room_name	async_function	lines 322-348	duplicate-found	room_get_state_event m.room.name room_put_state content name	src/mindroom/topic_generator.py:142; src/mindroom/matrix/avatar.py:180; src/mindroom/matrix/client_room_admin.py:322
add_room_to_space	async_function	lines 351-386	duplicate-found	room_get_state_event m.space.child room_put_state desired_content state_key	src/mindroom/hooks/state.py:31; src/mindroom/custom_tools/matrix_room.py:370; src/mindroom/matrix/client_room_admin.py:351
join_room	async_function	lines 389-402	none-found	client.join JoinResponse MatrixRoom rooms cache	src/mindroom/bot_room_lifecycle.py:101; src/mindroom/matrix/client_room_admin.py:389; none
get_room_members	async_function	lines 405-411	duplicate-found	joined_members JoinedMembersResponse member.user_id set	src/mindroom/thread_tags.py:570; src/mindroom/custom_tools/matrix_room.py:269; src/mindroom/authorization.py:275
get_joined_rooms	async_function	lines 414-420	none-found	joined_rooms JoinedRoomsResponse get_joined_rooms	src/mindroom/bot.py:664; src/mindroom/matrix/room_cleanup.py:177; src/mindroom/matrix/stale_stream_cleanup.py:134
get_room_name	async_function	lines 423-449	related-only	m.room.name room_get_state DM with Room with Unnamed Room	src/mindroom/api/matrix_operations.py:114; src/mindroom/custom_tools/matrix_room.py:250; src/mindroom/matrix/client_room_admin.py:423
leave_room	async_function	lines 452-459	none-found	room_leave RoomLeaveResponse leave_room	src/mindroom/api/matrix_operations.py:206; src/mindroom/matrix/rooms.py:706; src/mindroom/matrix/client_room_admin.py:452
```

Findings:

1. Repeated Matrix state read/compare/write helpers.
   - `src/mindroom/matrix/client_room_admin.py:90` reads `m.room.power_levels`, transforms content, compares, and writes it back.
   - `src/mindroom/matrix/client_room_admin.py:233` reads `m.room.join_rules`, compares one field, and writes the desired state event when needed.
   - `src/mindroom/matrix/client_room_admin.py:322` reads `m.room.name`, compares `content["name"]`, and writes the desired state event when needed.
   - `src/mindroom/matrix/client_room_admin.py:351` reads `m.space.child` with a state key, compares full content, and writes the desired state event when needed.
   - Similar Matrix state put/read flows appear in `src/mindroom/topic_generator.py:142`, `src/mindroom/matrix/avatar.py:180`, `src/mindroom/commands/config_confirmation.py:153`, `src/mindroom/thread_tags.py:486`, and `src/mindroom/scheduling.py:496`.
   - The duplicated behavior is the Matrix state-event wrapper shape: fetch state, validate response/content, optionally compare desired content, then `room_put_state` and convert `RoomPutStateResponse` into success/error handling.
   - Differences to preserve: each caller has distinct logging fields, missing-state behavior, malformed-content handling, state keys, and in some cases exception-vs-bool return semantics.

2. Joined-member fetching is duplicated with different payload needs.
   - `src/mindroom/matrix/client_room_admin.py:405` fetches joined members and returns a `set[str]`.
   - `src/mindroom/thread_tags.py:570` fetches joined members to assert requester membership.
   - `src/mindroom/custom_tools/matrix_room.py:269` fetches joined members and returns detailed member payloads plus optional power levels.
   - `src/mindroom/authorization.py:275` fetches joined members to refresh a cached `MatrixRoom`.
   - The shared behavior is the `client.joined_members(room_id)` call and `JoinedMembersResponse` validation.
   - Differences to preserve: callers need different result shapes and failure policy, so only a small low-level response helper would be safe.

3. `create_room` and `create_space` are related but not strong duplication.
   - `src/mindroom/matrix/client_room_admin.py:38` and `src/mindroom/matrix/client_room_admin.py:145` both build a `room_create` config from optional alias/topic and handle `RoomCreateResponse`.
   - The room path also configures power levels and invites power users, while the space path sets `space=True` and `private_chat`.
   - This is a small amount of colocated construction logic, and extracting it would likely reduce readability.

Proposed generalization:

Introduce no immediate production refactor for this task.
If the state-event duplication grows, the minimal safe helper would live in `src/mindroom/matrix/state_events.py` or near `client_room_admin.py` and provide one low-level function such as `put_state_event_bool(client, room_id, event_type, content, state_key=None) -> bool` plus a separate `get_state_event_content(...) -> dict[str, Any] | None`.
Avoid a broad "ensure state" abstraction until callers agree on failure policy, because current behavior intentionally varies between warnings/errors, bool returns, raised domain errors, and ignored write responses.

Risk/tests:

No production code was changed.
Any future helper extraction should cover:
- room admin unit tests for `ensure_thread_tags_power_level`, `ensure_room_join_rule`, `ensure_room_name`, and `add_room_to_space`;
- thread tag tests that rely on raised `ThreadTagsError`;
- scheduling/config confirmation tests where failed Matrix writes are currently tolerated or logged differently;
- authorization/thread-tag membership checks if joined-member fetching is shared.

Assumptions:

- Wrapper usage through `hooks/matrix_admin.py`, `matrix/rooms.py`, `api/matrix_operations.py`, and orchestrator call sites is not counted as duplication when it delegates to `client_room_admin.py`.
- The assignment asked for a report only, so no production code or tests were edited.
