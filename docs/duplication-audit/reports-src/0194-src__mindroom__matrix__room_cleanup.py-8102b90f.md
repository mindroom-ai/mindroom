Summary: No meaningful duplication found.
The closest related behavior is room membership reconciliation in `BotRoomLifecycle`, invitation reconciliation in `MultiAgentOrchestrator`, and shared Matrix admin wrappers, but `room_cleanup.py` is the only source module that identifies persisted managed bot accounts and kicks stale bot memberships from rooms.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_get_all_known_bot_usernames	function	lines 37-48	related-only	managed_account_usernames matrix_state account usernames INTERNAL_USER_ACCOUNT_KEY bot accounts	src/mindroom/matrix/state.py:122; src/mindroom/matrix/users.py:60; src/mindroom/matrix/identity.py:67; src/mindroom/thread_utils.py:55
_load_all_persisted_invited_rooms	function	lines 51-66	related-only	invited_room_entity_names should_persist_invited_rooms load_invited_rooms invited_rooms_path agent_username_localpart	src/mindroom/bot_room_lifecycle.py:78; src/mindroom/bot_room_lifecycle.py:82; src/mindroom/bot_room_lifecycle.py:86; src/mindroom/matrix/invited_rooms_store.py:21; src/mindroom/matrix/invited_rooms_store.py:77; src/mindroom/matrix/invited_rooms_store.py:82
_cleanup_orphaned_bots_in_room	async_function	lines 69-156	related-only	orphaned room cleanup room_kick configured_bot_usernames_for_room get_room_members is_dm_room leave_non_dm_rooms rooms_to_leave	src/mindroom/bot_room_lifecycle.py:121; src/mindroom/bot_room_lifecycle.py:129; src/mindroom/bot_room_lifecycle.py:147; src/mindroom/entity_resolution.py:18; src/mindroom/orchestrator.py:1636; src/mindroom/matrix/client_room_admin.py:405; src/mindroom/matrix/rooms.py:697
cleanup_all_orphaned_bots	async_function	lines 159-206	related-only	cleanup_all_orphaned_bots get_joined_rooms per-room cleanup joined_rooms summary kicked_bots	src/mindroom/bot.py:1154; src/mindroom/bot_room_lifecycle.py:98; src/mindroom/bot_room_lifecycle.py:129; src/mindroom/orchestrator.py:1621; src/mindroom/matrix/client_room_admin.py:414
```

Findings:

No real duplication found.

`_get_all_known_bot_usernames` is a small filter over the shared `managed_account_usernames()` helper.
`src/mindroom/matrix/state.py:122` already owns the Matrix state traversal, while `src/mindroom/matrix/users.py:60` reads one account's credential payload and `src/mindroom/matrix/identity.py:67` resolves a current configured agent name from a Matrix ID.
Those are related account-resolution operations, but none duplicate this function's specific behavior of collecting all non-internal persisted managed bot usernames for stale-membership detection.

`_load_all_persisted_invited_rooms` composes existing invited-room store helpers across all invite-owning entities.
`src/mindroom/bot_room_lifecycle.py:78`, `src/mindroom/bot_room_lifecycle.py:82`, and `src/mindroom/bot_room_lifecycle.py:86` perform the same persistence checks and file load for one bot runtime, and `src/mindroom/matrix/invited_rooms_store.py:77` enumerates eligible entity names.
The cleanup module's aggregate mapping by bot username is unique because it needs a cross-entity lookup while scanning room members.

`_cleanup_orphaned_bots_in_room` is related to `BotRoomLifecycle.rooms_to_leave()` and `leave_non_dm_rooms()` because all preserve DM rooms and compare current membership/rooms with configured rooms.
The behavior differs materially: lifecycle cleanup has a bot leave rooms from its own client, while orphaned-bot cleanup uses an admin client to inspect a room's members and kick other bot users only when they are persisted managed accounts, not configured for the room, and not preserved through invited-room state.
`src/mindroom/orchestrator.py:1636` also uses `configured_bot_usernames_for_room()` for the opposite flow, inviting missing configured bots.

`cleanup_all_orphaned_bots` is the only all-room driver for orphaned-bot kicking.
The joined-room enumeration pattern appears in room joining/leaving and invitation reconciliation, but those flows call different per-room actions and maintain different result shapes.

Proposed generalization:

No refactor recommended.
The related code already shares the low-level helpers that are worth sharing: `managed_account_usernames()`, invited-room persistence helpers, `configured_bot_usernames_for_room()`, `get_joined_rooms()`, `get_room_members()`, and `is_dm_room()`.
Extracting another helper for the remaining orchestration would either hide important policy differences or create a single-use abstraction.

Risk/tests:

No production code was changed.
If this area is refactored later, tests should preserve root-space skipping, DM-room preservation, persisted invited-room preservation, unknown/non-managed member ignoring, successful kick result aggregation, failed kick logging, and the all-room `None` joined-rooms case.
