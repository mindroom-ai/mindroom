Summary: Top duplication candidate is managed-room identifier expansion in `MatrixRoomAccessConfig.is_invite_only_room`, which mirrors authorization room-key lookup behavior.
Other symbols are either Pydantic configuration shape, thin config accessors, or consumers of config behavior rather than duplicated implementations.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MindRoomUserConfig	class	lines 30-67	related-only	mindroom_user username localpart validation reserved localparts	src/mindroom/config/main.py:855; src/mindroom/matrix/identity.py:138; src/mindroom/api/auth.py:219
MindRoomUserConfig.validate_username	method	lines 44-67	related-only	removeprefix @ localpart invalid characters cannot be empty	src/mindroom/config/main.py:855; src/mindroom/matrix/identity.py:138; src/mindroom/api/auth.py:219
MatrixSpaceConfig	class	lines 70-90	related-only	matrix_space name enabled root space alias	src/mindroom/config/main.py:886; src/mindroom/matrix/rooms.py:464
MatrixSpaceConfig.validate_name	method	lines 84-90	related-only	strip name cannot be empty field_validator	src/mindroom/thread_tags.py:53; src/mindroom/mcp/config.py:87; src/mindroom/api/schedules.py:207
MatrixDeliveryConfig	class	lines 93-104	related-only	ignore_unverified_devices matrix_delivery config consumers	src/mindroom/stop.py:380; src/mindroom/interactive.py:744; src/mindroom/matrix/client_delivery.py:218
ignore_unverified_devices_for_config	function	lines 107-109	none-found	ignore_unverified_devices_for_config direct matrix_delivery access	src/mindroom/approval_transport.py:270; src/mindroom/commands/config_confirmation.py:316; src/mindroom/custom_tools/matrix_api.py:826
MatrixRoomAccessConfig	class	lines 112-204	related-only	matrix_room_access join_rule directory_visibility invite_only_rooms	src/mindroom/matrix/rooms.py:92; src/mindroom/config_template.yaml:46; src/mindroom/cli/config.py:829
MatrixRoomAccessConfig.validate_unique_invite_only_rooms	method	lines 144-151	related-only	duplicate list validators len set duplicate items	src/mindroom/config/agent.py:349; src/mindroom/config/models.py:201; src/mindroom/config/auth.py:58
MatrixRoomAccessConfig.is_multi_user_mode	method	lines 153-155	none-found	mode == multi_user matrix_room_access	none
MatrixRoomAccessConfig.is_invite_only_room	method	lines 157-176	duplicate-found	room_alias_localpart managed_room_key_from_alias_localpart identifiers room_key room_id room_alias	src/mindroom/authorization.py:25; src/mindroom/matrix_identifiers.py:68
MatrixRoomAccessConfig.get_target_join_rule	method	lines 178-190	related-only	get_target_join_rule ensure_room_join_rule invite public knock	src/mindroom/matrix/rooms.py:92; src/mindroom/custom_tools/matrix_room.py:254
MatrixRoomAccessConfig.get_target_directory_visibility	method	lines 192-204	related-only	get_target_directory_visibility publish_to_room_directory private public	src/mindroom/matrix/rooms.py:98; src/mindroom/matrix/rooms.py:122
CacheConfig	class	lines 207-284	related-only	event cache backend db_path database_url namespace runtime identity	src/mindroom/runtime_support.py:75; src/mindroom/runtime_support.py:99; src/mindroom/matrix/cache/postgres_event_cache.py:665
CacheConfig.validate_database_url_env	method	lines 247-253	related-only	DATABASE_URL _DATABASE_URL runtime secret filter	src/mindroom/constants.py:602; src/mindroom/constants.py:617
CacheConfig.resolve_db_path	method	lines 255-259	related-only	resolve_db_path event_cache.db resolve_config_relative_path	src/mindroom/constants.py:849; src/mindroom/runtime_support.py:82
CacheConfig.resolve_postgres_database_url	method	lines 261-274	related-only	resolve postgres database_url env_value database_url_env	src/mindroom/runtime_support.py:86; src/mindroom/runtime_support.py:106; src/mindroom/workers/backends/kubernetes_config.py:146
CacheConfig.resolve_namespace	method	lines 276-284	related-only	resolve namespace MINDROOM_NAMESPACE default runtime_mindroom_namespace	src/mindroom/constants.py:815; src/mindroom/workers/backends/kubernetes_config.py:146; src/mindroom/tools/shell.py:226
```

Findings:

1. `MatrixRoomAccessConfig.is_invite_only_room` duplicates managed-room identifier expansion with `authorization._room_permission_lookup_keys`.
   In `src/mindroom/config/matrix.py:164`, the method builds an identifier set from `room_key`, optional `room_id`, optional `room_alias`, the alias localpart, and the namespace-stripped managed room key.
   In `src/mindroom/authorization.py:25`, `_room_permission_lookup_keys` performs the same alias-localpart and managed-room-key expansion for authorization map lookup.
   The outputs differ only in container type and ordering: config needs membership against `invite_only_rooms`, while authorization needs stable ordered keys and deduplicates with `dict.fromkeys`.
   Preserve that ordering requirement if generalized.

Related-only notes:

- Duplicate list validation appears in several config modules, including `src/mindroom/config/agent.py:349`, `src/mindroom/config/models.py:201`, and `src/mindroom/config/auth.py:58`.
  The behavior is a repeated pattern, but error wording and input types differ enough that extracting it from this file alone would have limited payoff.
- `CacheConfig.resolve_db_path`, `resolve_postgres_database_url`, and `resolve_namespace` are consumed by `src/mindroom/runtime_support.py:75` and `src/mindroom/runtime_support.py:99`.
  These are not duplicated there; runtime support correctly delegates to the config object.
- `CacheConfig.validate_database_url_env` delegates the actual env-name rule to `src/mindroom/constants.py:602`.
  No second validator reimplements the same `_DATABASE_URL` rule under `src`.
- `MatrixDeliveryConfig` is intentionally surfaced through `ignore_unverified_devices_for_config` and consumed in Matrix send paths.
  I found repeated calls to the helper, not repeated policy logic.

Proposed generalization:

Extract a small helper near the existing Matrix identifier helpers, for example `managed_room_lookup_keys(room_key, runtime_paths, *, room_id=None, room_alias=None) -> list[str]` in `src/mindroom/matrix_identifiers.py`.
Use it from both `MatrixRoomAccessConfig.is_invite_only_room` and `authorization._room_permission_lookup_keys`.
No broader config refactor recommended.

Risk/tests:

- Main behavior risk is changing lookup ordering for authorization or changing membership semantics for invite-only room checks.
- Tests should cover alias, alias localpart, namespaced managed room key, room ID, and direct room key matching for both invite-only room policy and authorization room permissions.
- Existing cache and Matrix delivery config behavior does not need duplication-driven refactoring tests unless those areas are changed separately.
