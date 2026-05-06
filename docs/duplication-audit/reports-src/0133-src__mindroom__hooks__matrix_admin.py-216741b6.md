## Summary

`src/mindroom/hooks/matrix_admin.py` is primarily a hook-facing adapter over existing Matrix admin helpers.
The most meaningful duplication is the repeated alias-resolution response handling also present in managed room and root-space reconciliation.
The create, invite, member-list, and space-link methods intentionally delegate to centralized helpers in `src/mindroom/matrix/client_room_admin.py`, so no refactor is recommended for those wrappers.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_BoundHookMatrixAdmin	class	lines 20-64	related-only	HookMatrixAdmin build_hook_matrix_admin BoundHook matrix_admin	src/mindroom/hooks/types.py:96; src/mindroom/hooks/context.py:185; src/mindroom/orchestrator.py:1000; src/mindroom/turn_controller.py:894; src/mindroom/scheduling.py:385
_BoundHookMatrixAdmin.resolve_alias	async_method	lines 26-31	duplicate-found	room_resolve_alias RoomResolveAliasResponse resolve alias	src/mindroom/matrix/rooms.py:277; src/mindroom/matrix/rooms.py:466
_BoundHookMatrixAdmin.create_room	async_method	lines 33-48	related-only	create_room alias_localpart power_user_ids power_users	src/mindroom/matrix/client_room_admin.py:38; src/mindroom/matrix/rooms.py:348
_BoundHookMatrixAdmin.invite_user	async_method	lines 50-52	related-only	invite_user invite_to_room room_invite RoomInviteResponse	src/mindroom/matrix/client_room_admin.py:24; src/mindroom/orchestrator.py:1505; src/mindroom/orchestrator.py:1527
_BoundHookMatrixAdmin.get_room_members	async_method	lines 54-56	related-only	get_room_members joined_members JoinedMembersResponse	src/mindroom/matrix/client_room_admin.py:405; src/mindroom/orchestrator.py:1501; src/mindroom/orchestrator.py:1558; src/mindroom/orchestrator.py:1641; src/mindroom/matrix/room_cleanup.py:104
_BoundHookMatrixAdmin.add_room_to_space	async_method	lines 58-64	related-only	add_room_to_space extract_server_name_from_homeserver m.space.child	src/mindroom/matrix/client_room_admin.py:351; src/mindroom/matrix/rooms.py:514; src/mindroom/matrix/rooms.py:516
build_hook_matrix_admin	function	lines 67-72	related-only	build_hook_matrix_admin _BoundHookMatrixAdmin HookMatrixAdmin	src/mindroom/hooks/__init__.py:162; src/mindroom/hooks/context.py:193; src/mindroom/orchestrator.py:1005; src/mindroom/turn_controller.py:899; src/mindroom/scheduling.py:385; src/mindroom/scheduling.py:1694
```

## Findings

### 1. Alias resolution repeats the same Matrix response pattern

`_BoundHookMatrixAdmin.resolve_alias` in `src/mindroom/hooks/matrix_admin.py:26` calls `client.room_resolve_alias(alias)`, checks for `nio.RoomResolveAliasResponse`, returns `str(response.room_id)`, and otherwise returns `None`.
The same response handling appears inside managed room reconciliation in `src/mindroom/matrix/rooms.py:277` and root-space reconciliation in `src/mindroom/matrix/rooms.py:466`.
Those call sites add state updates, join checks, and logging around the result, but the low-level "resolve alias to room ID or None" behavior is duplicated.

Differences to preserve:
`matrix_admin.py` accepts a fully formed alias and intentionally does no logging.
`matrix/rooms.py` constructs managed aliases from localparts and server names, logs successful managed resolution, and performs state reconciliation after the alias resolves.

### 2. Hook Matrix admin is an adapter over centralized room admin helpers

`_BoundHookMatrixAdmin.create_room`, `invite_user`, `get_room_members`, and `add_room_to_space` all delegate to `src/mindroom/matrix/client_room_admin.py`.
The apparent duplication is mostly naming adaptation from the hook protocol to the existing helper API:
`alias_localpart` maps to `alias`, `power_user_ids` maps to `power_users`, and `space_room_id` maps to `space_id`.
This is not a strong refactor target because the adapter preserves a smaller hook-facing protocol while keeping Matrix implementation details centralized.

### 3. Builder calls are repeated but intentional lifecycle wiring

`build_hook_matrix_admin` is called from hook context construction, command handling, scheduling, and orchestrator router-backed access.
These call sites all bind the same concrete adapter to the currently appropriate Matrix client.
The duplication is lifecycle wiring rather than duplicated behavior, and no shared abstraction would reduce meaningful complexity.

## Proposed Generalization

Add a small helper such as `resolve_room_alias(client: nio.AsyncClient, alias: str) -> str | None` in `src/mindroom/matrix/client_room_admin.py` or `src/mindroom/matrix/rooms.py` only if another caller needs alias resolution outside managed-room reconciliation.
Then `_BoundHookMatrixAdmin.resolve_alias` and the two managed-room/root-space resolution blocks could use the helper for the low-level response handling while preserving their surrounding state and logging logic.

No refactor is recommended for the create/invite/member/space-link wrappers because they already delegate to the central Matrix admin helper module.

## Risk/tests

Risk is low for extracting alias resolution if the helper remains a pure thin wrapper over `client.room_resolve_alias`.
Tests should cover success with `nio.RoomResolveAliasResponse`, failure/error response returning `None`, and managed room/root-space reconciliation still performing their existing state updates after a successful resolution.
No production code was edited for this audit.
