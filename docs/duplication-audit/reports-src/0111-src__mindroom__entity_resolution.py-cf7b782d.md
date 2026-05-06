## Summary

Top duplication candidates are Matrix domain/user ID derivation outside `entity_resolution.py` and repeated room-membership resolution loops for "which configured agents belong to this room".
The rest of the module is mostly canonical helper logic delegated through `Config` methods, with wrappers and call sites rather than independent duplicate behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
configured_bot_usernames_for_room	function	lines 18-39	duplicate-found	configured bots for room, resolve_room_aliases agents teams, get agent ids for room	src/mindroom/thread_utils.py:217; src/mindroom/agents.py:1276; src/mindroom/matrix/room_cleanup.py:109; src/mindroom/orchestrator.py:1637
matrix_domain	function	lines 42-45	duplicate-found	runtime_matrix_homeserver extract_server_name_from_homeserver get_domain server_name	src/mindroom/orchestrator.py:1551; src/mindroom/orchestrator.py:1625; src/mindroom/matrix/users.py:109; src/mindroom/matrix/users.py:504; src/mindroom/matrix/rooms.py:555; src/mindroom/config/main.py:902
entity_matrix_ids	function	lines 48-58	related-only	MatrixID.from_agent config agents teams router get_ids configured sender ids	src/mindroom/matrix/identity.py:281; src/mindroom/bot.py:202; src/mindroom/agents.py:1086; src/mindroom/config/main.py:908
mindroom_user_id	function	lines 61-65	duplicate-found	mindroom_user MatrixID.from_username config.get_mindroom_user_id internal user id	src/mindroom/orchestrator.py:1548; src/mindroom/matrix/identity.py:291; src/mindroom/orchestration/rooms.py:49; src/mindroom/tool_approval.py:194
resolve_agent_thread_mode	function	lines 68-97	related-only	room_thread_modes get_entity_thread_mode thread mode room alias overrides	src/mindroom/config/main.py:1596; src/mindroom/conversation_resolver.py:188; src/mindroom/delivery_gateway.py:582; src/mindroom/response_runner.py:1513
router_agents_for_room	function	lines 100-118	related-only	router agents room team agents resolve_room_aliases get_entity_thread_mode	src/mindroom/config/main.py:1631; src/mindroom/thread_utils.py:217; src/mindroom/agents.py:1276
effective_entity_model_name	function	lines 121-134	related-only	room_models get_effective_entity_model_name resolve_runtime_model select_model_for_team	src/mindroom/config/main.py:1683; src/mindroom/config/main.py:1694; src/mindroom/teams.py:1398; src/mindroom/ai_run_metadata.py:42
```

## Findings

1. Matrix domain and internal user ID derivation are repeated outside the canonical helpers.

`matrix_domain()` centralizes `runtime_matrix_homeserver(runtime_paths)` plus `extract_server_name_from_homeserver(...)` at `src/mindroom/entity_resolution.py:42`.
The same two-step domain derivation appears in `src/mindroom/orchestrator.py:1551` and `src/mindroom/orchestrator.py:1625`.
`mindroom_user_id()` centralizes the configured internal user check plus `MatrixID.from_username(...).full_id` at `src/mindroom/entity_resolution.py:61`.
`src/mindroom/orchestrator.py:1548` repeats the same behavior for the persisted internal account username, with the important difference that it intentionally reads the username from `matrix_state.yaml` rather than directly from `config.mindroom_user`.
Related call sites in `src/mindroom/matrix/identity.py:291`, `src/mindroom/orchestration/rooms.py:49`, and `src/mindroom/tool_approval.py:194` already use `config.get_mindroom_user_id(...)`, so the duplication is localized rather than widespread.

2. Room membership selection loops are duplicated for configured agent visibility.

`configured_bot_usernames_for_room()` resolves each agent and team room list and returns the matching bot localparts, adding the router when any configured bot is present at `src/mindroom/entity_resolution.py:18`.
`src/mindroom/thread_utils.py:217` independently resolves every agent's rooms to produce agent `MatrixID`s configured for a room, excluding the router and teams.
`router_agents_for_room()` repeats a similar room-resolution loop at `src/mindroom/entity_resolution.py:100`, but it returns agent names and expands teams into member agents for router policy fallback.
`src/mindroom/agents.py:1276` has a related but weaker variant for authored room keys only, not resolved room IDs, and it only includes direct agents plus router.
The shared behavior is "walk configured entities, resolve room aliases, and test room membership"; the differences are output shape, router inclusion, and whether teams are returned as team bots or expanded to member agents.

3. Entity ID and model helpers are already mostly centralized.

`entity_matrix_ids()` is delegated by `Config.get_ids()` in `src/mindroom/config/main.py:908`, and most call sites use that interface.
`src/mindroom/matrix/identity.py:281` builds persisted-account sender IDs for active accounts with similar entity enumeration, but it is keyed by persisted account key and includes `agent_user`, so it is related rather than a direct duplicate.
`effective_entity_model_name()` is delegated by `Config.get_effective_entity_model_name()` and `Config.resolve_runtime_model()` in `src/mindroom/config/main.py:1683` and `src/mindroom/config/main.py:1694`.
`src/mindroom/teams.py:1398` uses the centralized resolver and only repeats the room alias check for logging branch selection, so no refactor is recommended there.
`resolve_agent_thread_mode()` appears to be the canonical implementation for room-specific thread-mode overrides; other hits call through `Config.get_entity_thread_mode(...)`.

## Proposed Generalization

1. Consider replacing repeated domain derivation in `orchestrator.py` with `config.get_domain(self.runtime_paths)` or `matrix_domain(self.runtime_paths)` where no client-specific homeserver is required.
Preserve call sites that derive a server name from a non-runtime homeserver, such as Matrix client homeserver helpers.

2. If room membership logic changes again, introduce a small internal helper in `entity_resolution.py`, for example `_entity_configured_in_room(room_keys, room_id, runtime_paths) -> bool`, and reuse it from `configured_bot_usernames_for_room()` and `router_agents_for_room()`.
Do not broaden it into a public abstraction unless `thread_utils.py` or `agents.py` are also updated, because their output semantics differ.

3. No refactor is recommended for `resolve_agent_thread_mode()`, `entity_matrix_ids()`, or `effective_entity_model_name()` based on this audit.

## Risk/tests

Changing domain derivation in orchestrator invitation flow risks inviting the wrong Matrix user if persisted usernames differ from authored config usernames.
Tests should cover internal-user invitation with a persisted username and a runtime `MATRIX_SERVER_NAME` override.

Any shared room-membership helper should be tested with room IDs, aliases, unresolved authored room keys, team rooms, router inclusion, and fallback behavior when no room-specific router agents match.
