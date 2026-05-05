## Summary

Top duplication candidate: `src/mindroom/api/matrix_operations.py` repeats the same configured-vs-joined Matrix room difference calculation that `src/mindroom/bot_room_lifecycle.py` already performs for bot room cleanup.
The API version intentionally includes DM rooms and room names for dashboard display, while the lifecycle version filters DMs before leaving, so any extraction should preserve those differences.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
RoomLeaveRequest	class	lines 21-25	not-a-behavior-symbol	RoomLeaveRequest agent_id room_id request model	tests/api/test_matrix_operations.py:164; tests/api/test_matrix_operations.py:219; tests/api/test_matrix_operations.py:245
_RoomInfo	class	lines 28-32	not-a-behavior-symbol	_RoomInfo room_id name room details get_room_name	src/mindroom/api/matrix_operations.py:112; src/mindroom/matrix/client_room_admin.py:341
AgentRoomsResponse	class	lines 35-43	not-a-behavior-symbol	AgentRoomsResponse configured_rooms joined_rooms unconfigured_rooms	tests/api/test_matrix_operations.py:73; tests/api/test_matrix_operations.py:109; tests/api/test_matrix_operations.py:137
AllAgentsRoomsResponse	class	lines 46-49	not-a-behavior-symbol	AllAgentsRoomsResponse agents list response	tests/api/test_matrix_operations.py:73; tests/api/test_matrix_operations.py:75
_get_configured_matrix_entities	function	lines 52-57	related-only	config agents teams configured entities entity_matrix_ids agents teams	src/mindroom/entity_resolution.py:48; src/mindroom/matrix/users.py:801; src/mindroom/matrix/users.py:817; src/mindroom/orchestrator.py:724
_get_configured_matrix_entity	function	lines 60-68	related-only	agent or team not found entity lookup agents teams	src/mindroom/agents.py:1293; src/mindroom/bot.py:196; src/mindroom/bot.py:219; src/mindroom/entity_resolution.py:128
_get_agent_matrix_rooms	async_function	lines 71-126	duplicate-found	get_joined_rooms resolve_room_aliases unconfigured_rooms get_room_name rooms_to_leave	src/mindroom/bot_room_lifecycle.py:129; src/mindroom/bot_room_lifecycle.py:147; src/mindroom/bot.py:762; src/mindroom/agents.py:1293; src/mindroom/bot.py:160
get_all_agents_rooms	async_function	lines 130-148	related-only	read_committed_config_and_runtime asyncio.gather all agents rooms	tests/api/test_matrix_operations.py:53; src/mindroom/api/matrix_operations.py:71; src/mindroom/api/openai_compat.py:873
get_agent_rooms	async_function	lines 152-170	related-only	get one configured agent rooms read_committed_config_and_runtime	tests/api/test_matrix_operations.py:91; tests/api/test_matrix_operations.py:285; src/mindroom/api/matrix_operations.py:71
leave_room_endpoint	async_function	lines 174-213	related-only	create_agent_user login_agent_user leave_room client.close leave non dm	src/mindroom/bot_room_lifecycle.py:121; src/mindroom/matrix/rooms.py:559; src/mindroom/api/schedules.py:235; tests/api/test_matrix_operations.py:151
leave_rooms_bulk	async_function	lines 217-244	related-only	bulk leave partial failure HTTPException results success	tests/api/test_matrix_operations.py:227; tests/api/test_matrix_operations.py:253; src/mindroom/api/matrix_operations.py:174
```

## Findings

### 1. Configured-vs-joined room difference is duplicated

`src/mindroom/api/matrix_operations.py:99-115` fetches joined rooms, resolves configured room aliases, computes rooms joined but not configured, and optionally enriches those rooms with names.
`src/mindroom/bot_room_lifecycle.py:129-151` fetches joined rooms, builds configured rooms, computes `current_rooms - configured_rooms`, and then filters DMs in `rooms_to_actually_leave`.

These are functionally the same core behavior: compare Matrix membership against configured room membership for one entity.
The differences to preserve are important:

- The API reads raw config dictionaries from the committed API snapshot and handles both agents and teams through `_get_configured_matrix_entities`.
- The bot lifecycle uses typed `Config`, includes persisted invited rooms, includes the router root space, and filters DMs before actual leave.
- The API intentionally reports unconfigured DM rooms because dashboard users may need to see them; lifecycle cleanup intentionally avoids leaving DMs.
- The API enriches only unconfigured rooms with `get_room_name`.

### 2. Matrix entity enumeration is related but not duplicate enough to extract alone

`src/mindroom/api/matrix_operations.py:52-68` merges `config_data["agents"]` and `config_data["teams"]` and raises a 404 for missing IDs.
Related typed entity resolution appears in `src/mindroom/entity_resolution.py:48-58`, `src/mindroom/agents.py:1293-1316`, and `src/mindroom/bot.py:160-230`.

This is similar domain logic, but not a direct duplicate because the API works on raw committed config data to avoid stale runtime snapshots, while the other call sites use typed `Config`.
Extracting a shared helper now would either weaken typing or add a second raw-config abstraction with little payoff.

### 3. Matrix client acquisition and close flow is repeated but entity-specific

`src/mindroom/api/matrix_operations.py:87-98` and `src/mindroom/api/matrix_operations.py:192-204` both resolve the homeserver, create/login an entity Matrix user, and later close the client.
`src/mindroom/api/schedules.py:235-244` does the same shape for the router user, and closes in `finally` at `src/mindroom/api/schedules.py:264-275`, `src/mindroom/api/schedules.py:291-311`, and `src/mindroom/api/schedules.py:324-337`.

This is related lifecycle behavior, but the duplication is modest and currently spread across distinct API responsibilities.
The more concrete issue is that `matrix_operations.py` closes manually after successful operations rather than using `try/finally`, so exceptions from `get_joined_rooms`, `get_room_name`, or `leave_room` could skip client cleanup.
That is a reliability risk, but not primarily a duplication refactor.

## Proposed Generalization

Minimal helper recommended only for the room-difference behavior:

1. Add a small pure helper near existing room lifecycle utilities, for example `mindroom.matrix.rooms.diff_joined_configured_rooms(joined_rooms: Iterable[str], configured_rooms: Iterable[str]) -> list[str]`.
2. Use it from `matrix_operations._get_agent_matrix_rooms` for API reporting.
3. Use it from `BotRoomLifecycle.rooms_to_leave` before preserving lifecycle-specific additions and DM filtering.
4. Keep room-name enrichment in the API and DM filtering in `BotRoomLifecycle.rooms_to_actually_leave`.
5. Add focused tests for preserving duplicate joined-room ordering, configured alias resolution staying outside the helper, and DM inclusion/exclusion differences.

No refactor recommended for the Pydantic models, raw entity lookup, endpoint wrappers, or bulk leave handling.

## Risk/Tests

The main behavior risk is changing which rooms are considered unconfigured.
Tests should cover agents, teams, configured aliases resolved to room IDs, joined rooms that are DMs, and router/root-space behavior if the helper is used in lifecycle cleanup.

The existing API tests in `tests/api/test_matrix_operations.py` cover response shape, teams, partial bulk failure, and stale config snapshot handling.
Any implementation should keep those tests and add bot lifecycle coverage around `rooms_to_leave` and `rooms_to_actually_leave` so the API can continue reporting DMs while automated cleanup still avoids leaving them.
