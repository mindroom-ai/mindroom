Summary: The main duplication candidate is room identifier expansion for room IDs, aliases, alias localparts, and managed room keys, repeated in authorization and Matrix room-access config.
Sender original-author extraction is related duplicated scalar logic, but the authorization helper is already the central effective-requester implementation for permission checks.
No broader refactor is recommended for agent availability or authoritative membership refresh because those functions are already used as the shared authorization-facing implementation.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_room_permission_lookup_keys	function	lines 27-46	duplicate-found	room_alias_localpart managed_room_key_from_alias_localpart identifiers invite_only_rooms room_permissions	src/mindroom/config/matrix.py:157; src/mindroom/matrix_identifiers.py:52; src/mindroom/matrix_identifiers.py:68
_lookup_managed_room_identifiers	function	lines 49-58	related-only	matrix_state_for_runtime state.rooms room.room_id room.alias get_room_alias_from_id	src/mindroom/matrix/rooms.py:397; src/mindroom/matrix/state.py:98
is_authorized_sender	function	lines 61-113	related-only	is_authorized_sender global_users room_permissions default_room_access resolve_alias	src/mindroom/bot.py:1423; src/mindroom/turn_controller.py:308; src/mindroom/approval_inbound.py:82; src/mindroom/custom_tools/attachment_helpers.py:38; src/mindroom/commands/handler.py:224
is_sender_allowed_for_agent_reply	function	lines 116-142	related-only	agent_reply_permissions fnmatchcase sender allowed reply permissions resolve_alias	src/mindroom/thread_utils.py:301; src/mindroom/turn_policy.py:253; src/mindroom/authorization.py:173
get_effective_sender_id_for_reply_permissions	function	lines 145-170	related-only	ORIGINAL_SENDER_KEY original_sender effective requester sender content	src/mindroom/turn_controller.py:209; src/mindroom/execution_preparation.py:298; src/mindroom/execution_preparation.py:306; src/mindroom/coalescing_batch.py:96; src/mindroom/matrix/stale_stream_cleanup.py:1059
filter_agents_by_sender_permissions	function	lines 173-185	none-found	filter_agents_by_sender_permissions is_sender_allowed_for_agent_reply agent_name config runtime_paths	src/mindroom/thread_utils.py:194; src/mindroom/thread_utils.py:328; src/mindroom/turn_controller.py:1033
_available_agents_from_member_ids	function	lines 188-200	related-only	MatrixID.parse member_ids agent_name ROUTER_AGENT_NAME sorted full_id	src/mindroom/thread_utils.py:205; src/mindroom/matrix/room_cleanup.py:118; src/mindroom/scheduling.py:1234; src/mindroom/commands/handler.py:135
get_available_agents_in_room	function	lines 203-212	none-found	get_available_agents_in_room room.users available agents router excluded	src/mindroom/teams.py:814; src/mindroom/custom_tools/config_manager.py:468; src/mindroom/authorization.py:188
get_available_agents_for_sender	function	lines 215-227	none-found	get_available_agents_for_sender available_agents sender permissions room users	src/mindroom/thread_utils.py:314; src/mindroom/turn_policy.py:292; src/mindroom/authorization.py:203
_apply_authoritative_joined_members	function	lines 230-254	none-found	members_synced add_member remove_member JoinedMembersResponse authoritative joined members	src/mindroom/matrix/client_room_admin.py:405; src/mindroom/thread_tags.py:563; src/mindroom/custom_tools/matrix_room.py:269
get_available_agents_for_sender_authoritative	async_function	lines 257-304	related-only	joined_members JoinedMembersResponse members_synced available_agents_for_sender_authoritative	src/mindroom/matrix/client_room_admin.py:405; src/mindroom/thread_tags.py:563; src/mindroom/custom_tools/matrix_room.py:269; src/mindroom/scheduling.py:1268; src/mindroom/voice_handler.py:502
```

## Findings

### 1. Room identifier expansion is duplicated

- `src/mindroom/authorization.py:27` builds ordered lookup keys from `room_id`, optional `room_key`, optional `room_alias`, alias localpart, and managed room key.
- `src/mindroom/config/matrix.py:157` builds the same conceptual identifier set for `MatrixRoomAccessConfig.is_invite_only_room`, using `room_key`, optional `room_id`, optional `room_alias`, alias localpart, and managed room key.

These are functionally the same room-identifier normalization step applied to two policy maps: authorization room permissions and invite-only room access.
The main difference is output shape.
Authorization preserves lookup order and deduplicates with `dict.fromkeys`; room access only needs membership against `invite_only_rooms`.

### 2. Original sender extraction is repeated around the centralized permission helper

- `src/mindroom/authorization.py:145` resolves the effective sender for reply permissions by trusting `ORIGINAL_SENDER_KEY` only when the transport sender is an active internal MindRoom identity.
- `src/mindroom/turn_controller.py:209` has an extra direct `ORIGINAL_SENDER_KEY` branch for the current bot sender before delegating back to the authorization helper.
- `src/mindroom/execution_preparation.py:298` and `src/mindroom/execution_preparation.py:306` extract the same `ORIGINAL_SENDER_KEY` value from visible message content for speaker labeling and relayed-message checks.
- `src/mindroom/coalescing_batch.py:96` scans trusted pending-event content for the same key.

This is related duplicated scalar extraction, but not all call sites enforce the same trust boundary.
The permission-sensitive path is already centralized in `get_effective_sender_id_for_reply_permissions`.
Any future helper should separate raw extraction from trusted effective-sender resolution.

## Proposed Generalization

1. Add a small room identifier helper near `src/mindroom/matrix_identifiers.py`, for example `room_identifier_keys(room_key=None, room_id=None, room_alias=None, runtime_paths=...) -> tuple[str, ...]`.
2. Preserve insertion order for authorization lookups and allow config callers to convert the tuple to a set when only membership is needed.
3. Replace `_room_permission_lookup_keys` internals and `MatrixRoomAccessConfig.is_invite_only_room` identifier construction with that helper.
4. Keep `_lookup_managed_room_identifiers` separate; it performs persisted-state lookup rather than identifier normalization.
5. Do not refactor agent availability or authoritative membership functions now; they are already the central implementation used by callers.

No production-code changes were made for this audit.

## Risk/tests

- Room identifier helper risk: changing order could alter which authorization entry wins when multiple keys are configured for the same room.
- Tests should cover room permission lookup by room ID, full alias, alias localpart, managed room key, and duplicate identifier deduplication.
- Tests should also cover invite-only room matching by the same identifier variants.
- Original-sender extraction should not be generalized without tests proving untrusted external senders cannot spoof `com.mindroom.original_sender` for reply permissions.
