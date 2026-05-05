# Summary

Top duplication candidates for `src/mindroom/bot_room_lifecycle.py`:

- `src/mindroom/bot.py` still exposes thin lifecycle wrapper methods that duplicate the public method surface of `BotRoomLifecycle`.
- Configured-vs-joined room difference logic appears both in `BotRoomLifecycle.rooms_to_leave` and the Matrix operations API status endpoint, with different configured-room inputs.
- Invited-room persistence is already centralized in `mindroom.matrix.invited_rooms_store`; lifecycle methods mostly gate and delegate to that module, so no further storage duplication is active here.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
BotRoomLifecycleDeps	class	lines 37-48	not-a-behavior-symbol	BotRoomLifecycleDeps dependency bundle get_configured_rooms callbacks	src/mindroom/bot.py:319
BotRoomLifecycle	class	lines 51-223	related-only	room lifecycle invite join leave welcome	src/mindroom/bot.py:805; src/mindroom/matrix/rooms.py:697; src/mindroom/matrix/room_cleanup.py:69
BotRoomLifecycle.__init__	method	lines 57-59	related-only	load invited rooms init lifecycle _invited_rooms	src/mindroom/bot.py:319; src/mindroom/bot.py:332
BotRoomLifecycle._client	method	lines 61-66	related-only	client None Matrix client not ready assert client	src/mindroom/bot.py:661; src/mindroom/bot.py:834; src/mindroom/bot.py:1272
BotRoomLifecycle._config	method	lines 68-69	not-a-behavior-symbol	runtime config property delegation	none
BotRoomLifecycle._logger	method	lines 71-72	not-a-behavior-symbol	get_logger logger delegation	none
BotRoomLifecycle.should_accept_invite	method	lines 74-76	duplicate-found	should_accept_invite should_accept_invites accept_invites	src/mindroom/bot.py:805; src/mindroom/matrix/invited_rooms_store.py:65
BotRoomLifecycle.should_persist_invited_rooms	method	lines 78-80	duplicate-found	should_persist_invited_rooms persist invited rooms	src/mindroom/bot.py:809; src/mindroom/matrix/invited_rooms_store.py:82; src/mindroom/matrix/room_cleanup.py:58
BotRoomLifecycle.invited_rooms_file_path	method	lines 82-84	duplicate-found	invited_rooms_path invited_rooms.json	src/mindroom/bot.py:813; src/mindroom/matrix/invited_rooms_store.py:21; src/mindroom/matrix/room_cleanup.py:62
BotRoomLifecycle.load_invited_rooms	method	lines 86-90	duplicate-found	load_invited_rooms should_persist_invited_rooms	src/mindroom/bot.py:817; src/mindroom/matrix/room_cleanup.py:51; src/mindroom/matrix/invited_rooms_store.py:26
BotRoomLifecycle.save_invited_rooms	method	lines 92-96	duplicate-found	save_invited_rooms invited_rooms	src/mindroom/bot.py:821; src/mindroom/matrix/invited_rooms_store.py:49
BotRoomLifecycle.join_configured_rooms	async_method	lines 98-119	related-only	join configured rooms desired current get_joined_rooms join_room	src/mindroom/bot.py:825; src/mindroom/matrix/rooms.py:459; src/mindroom/matrix/rooms.py:568
BotRoomLifecycle.leave_unconfigured_rooms	async_method	lines 121-127	related-only	leave unconfigured rooms leave_non_dm_rooms	src/mindroom/bot.py:860; src/mindroom/matrix/rooms.py:697
BotRoomLifecycle.rooms_to_leave	async_method	lines 129-145	duplicate-found	rooms_to_leave unconfigured rooms joined configured root space	src/mindroom/bot.py:860; src/mindroom/api/matrix_operations.py:99; src/mindroom/matrix/room_cleanup.py:91
BotRoomLifecycle.rooms_to_actually_leave	async_method	lines 147-151	related-only	is_dm_room filter rooms to leave leave_non_dm_rooms	src/mindroom/matrix/rooms.py:697; src/mindroom/matrix/room_cleanup.py:98
BotRoomLifecycle.send_welcome_message_if_empty	async_method	lines 153-187	related-only	room_messages welcome message empty room _generate_welcome_message	src/mindroom/bot.py:1249; src/mindroom/commands/handler.py:127; src/mindroom/commands/handler.py:238
BotRoomLifecycle.on_invite	async_method	lines 189-223	related-only	InviteEvent is_authorized_sender canonical_alias join_room invited_rooms	src/mindroom/bot.py:1330; src/mindroom/approval_inbound.py:82; src/mindroom/turn_controller.py:308; src/mindroom/bot.py:1423
```

# Findings

## 1. Thin lifecycle wrappers remain in `bot.py`

`src/mindroom/bot.py:805`, `src/mindroom/bot.py:809`, `src/mindroom/bot.py:813`, `src/mindroom/bot.py:817`, `src/mindroom/bot.py:821`, `src/mindroom/bot.py:825`, `src/mindroom/bot.py:860`, `src/mindroom/bot.py:1249`, and `src/mindroom/bot.py:1330` delegate directly to `BotRoomLifecycle`.
These wrappers duplicate the lifecycle public API names and docstrings without adding behavior, except `leave_unconfigured_rooms`, which adds the "no rooms to leave" early return before delegating.

Differences to preserve:

- `bot.py:860` computes `rooms_to_actually_leave()` once and skips the leave call when the result is empty.
- Other wrappers may exist for compatibility with internal call sites or tests that still address `AgentBot` methods.

## 2. Joined-vs-configured room diff logic is repeated for status reporting

`BotRoomLifecycle.rooms_to_leave` at `src/mindroom/bot_room_lifecycle.py:129` fetches joined rooms, builds a configured-room set, adds persisted invited rooms, preserves the router root space, and returns `current_rooms - configured_rooms`.
`src/mindroom/api/matrix_operations.py:99` fetches joined rooms, resolves configured rooms, and calculates unconfigured rooms with `[room for room in joined_rooms if room not in configured_room_ids]`.

The behaviors are related but not identical.
The API endpoint reports room state for one agent based on raw request data and resolved aliases, while `BotRoomLifecycle` enforces runtime leave policy and includes invited-room and root-space preservation.

Differences to preserve:

- Lifecycle leave policy must include persisted invited rooms when enabled.
- Router lifecycle must preserve the root Matrix Space.
- API status output preserves joined-room ordering and returns room details.

## 3. Invited-room storage behavior is centralized, with lifecycle-specific gates

`BotRoomLifecycle` methods at `src/mindroom/bot_room_lifecycle.py:74`, `src/mindroom/bot_room_lifecycle.py:78`, `src/mindroom/bot_room_lifecycle.py:82`, `src/mindroom/bot_room_lifecycle.py:86`, and `src/mindroom/bot_room_lifecycle.py:92` delegate to `src/mindroom/matrix/invited_rooms_store.py:21`, `src/mindroom/matrix/invited_rooms_store.py:26`, `src/mindroom/matrix/invited_rooms_store.py:49`, `src/mindroom/matrix/invited_rooms_store.py:65`, and `src/mindroom/matrix/invited_rooms_store.py:82`.
`src/mindroom/matrix/room_cleanup.py:51` uses the same helpers to preserve invited rooms during orphan cleanup.

This is not harmful production duplication because the storage and policy rules already have one source of truth.
The remaining duplicate-looking methods in `BotRoomLifecycle` are lifecycle instance adapters that bind `config`, `runtime_paths`, and `agent_name`.

# Proposed Generalization

No broad refactor recommended.

A small cleanup could remove or inline the thin wrapper methods in `AgentBot` once no call sites or tests depend on them.
For room-diff behavior, a minimal helper such as `mindroom.matrix.room_membership.diff_unconfigured_rooms(joined_rooms, configured_rooms, preserved_rooms=())` could reduce repeated set/list comparison, but only if more active callers need the same semantics.
Do not merge lifecycle leave policy and API status reporting unless the API should intentionally report the same preserved-room policy.

# Risk/Tests

Risks:

- Removing `AgentBot` wrappers could break tests or external internal callers that patch or call those methods directly.
- Sharing room-diff helpers could accidentally change ordering in API responses or remove lifecycle-only preservation of invited rooms and the root Space.
- Welcome-message logic is router-specific and depends on Matrix pagination shape; it should remain local unless another caller needs the same empty-room check.

Tests to review for any future refactor:

- Bot room lifecycle tests covering invite acceptance, persistence, configured joins, unconfigured leaves, DM preservation, and router root-space preservation.
- API matrix operations tests covering configured, joined, and unconfigured room output ordering.
- Bot wrapper tests or call sites that patch `AgentBot.join_configured_rooms`, `AgentBot.leave_unconfigured_rooms`, or `_on_invite`.
