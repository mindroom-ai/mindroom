## Summary

Top duplication candidates for `src/mindroom/matrix/state.py`:

1. `_current_runtime_domain` duplicates Matrix homeserver server-name extraction already implemented by `matrix_identifiers.extract_server_name_from_homeserver`.
2. `_write_matrix_state_file` and `_fsync_directory` repeat the same atomic temp-file, flush, replace, and directory fsync flow used by handled-turn and interactive persistence.
3. `_load_matrix_state_file` repeats the repository's YAML `safe_load(...) or {}` pattern, but its Matrix-specific validation, migration, and normalization make this related rather than an immediate refactor target.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_MatrixAccount	class	lines 15-22	related-only	"_MatrixAccount access_token device_id domain accounts add_account"	src/mindroom/matrix/users.py:113, src/mindroom/avatar_generation.py:336, src/mindroom/matrix/client_session.py:138
MatrixRoom	class	lines 25-36	related-only	"MatrixRoom room_id alias name created_at load_rooms"	src/mindroom/matrix/rooms.py:167, src/mindroom/matrix/rooms.py:188, src/mindroom/matrix/rooms.py:286
MatrixRoom.serialize_datetime	method	lines 34-36	none-found	"field_serializer created_at isoformat model_dump mode json datetime serialization"	src/mindroom/knowledge/registry.py:125, src/mindroom/thread_summary.py:419, src/mindroom/scheduling.py:529
MatrixState	class	lines 39-104	related-only	"MatrixState accounts rooms space_room_id load save"	src/mindroom/matrix/rooms.py:167, src/mindroom/matrix/rooms.py:458, src/mindroom/avatar_generation.py:446
MatrixState.load	method	lines 47-55	related-only	"MatrixState.load model_copy deep cached mutable state"	src/mindroom/matrix/rooms.py:167, src/mindroom/knowledge/refresh_scheduler.py:147, src/mindroom/hooks/execution.py:154
MatrixState.save	method	lines 57-63	related-only	"model_dump mode json save yaml state file"	src/mindroom/scheduling.py:440, src/mindroom/tool_system/output_files.py:349, src/mindroom/workers/runtime.py:42
MatrixState.get_account	method	lines 65-67	none-found	"get_account accounts.get MatrixState account lookup"	src/mindroom/matrix/rooms.py:549, src/mindroom/avatar_generation.py:447
MatrixState.add_account	method	lines 69-88	related-only	"add_account existing domain preserve device_id access_token"	src/mindroom/matrix/users.py:113, src/mindroom/avatar_generation.py:349
MatrixState.get_room	method	lines 90-92	none-found	"get_room rooms.get room_id lookup"	src/mindroom/matrix/rooms.py:184
MatrixState.add_room	method	lines 94-96	related-only	"add_room MatrixRoom datetime.now UTC room state"	src/mindroom/matrix/rooms.py:196, src/mindroom/matrix/rooms.py:286, src/mindroom/matrix/rooms.py:358
MatrixState.get_room_aliases	method	lines 98-100	related-only	"get_room_aliases alias mapping resolve room aliases"	src/mindroom/matrix/rooms.py:177, src/mindroom/matrix/rooms.py:225, src/mindroom/matrix/rooms.py:240
MatrixState.set_space_room_id	method	lines 102-104	related-only	"set_space_room_id space_room_id save root space"	src/mindroom/matrix/rooms.py:477, src/mindroom/matrix/rooms.py:491, src/mindroom/matrix/room_cleanup.py:94
matrix_state_for_runtime	function	lines 107-119	related-only	"matrix_state_for_runtime cached state read-only runtime paths"	src/mindroom/matrix/rooms.py:177, src/mindroom/authorization.py:54, src/mindroom/avatar_generation.py:446
managed_account_usernames	function	lines 122-125	related-only	"managed_account_usernames agent_ persisted usernames"	src/mindroom/matrix/room_cleanup.py:46, src/mindroom/matrix/identity.py:76, src/mindroom/matrix/identity.py:301
_matrix_state_cache_key	function	lines 128-133	none-found	"st_mtime_ns st_size lru cache key file exists stat"	src/mindroom/tool_approval.py:128, src/mindroom/knowledge/manager.py:1260, src/mindroom/tool_system/skills.py:351
_load_matrix_state_file_cached	function	lines 137-146	none-found	"lru_cache maxsize 64 file mtime size cached load"	src/mindroom/matrix/identity.py:117
_current_runtime_domain	function	lines 149-156	duplicate-found	"runtime_matrix_server_name runtime_matrix_homeserver split :// server name"	src/mindroom/matrix_identifiers.py:75
_migrate_accounts_to_current_schema	function	lines 159-166	none-found	"migrate accounts domain current_domain normalize schema"	none
_load_matrix_state_file	function	lines 169-180	related-only	"yaml.safe_load or {} model_validate model_dump normalize rewrite"	src/mindroom/config/main.py:965, src/mindroom/config/main.py:1761, src/mindroom/api/sandbox_runner.py:159
_write_matrix_state_file	function	lines 183-204	duplicate-found	"NamedTemporaryFile safe_dump flush fsync replace temp unlink atomic write"	src/mindroom/handled_turns.py:407, src/mindroom/interactive.py:189, src/mindroom/tool_system/output_files.py:415
_fsync_directory	function	lines 207-216	duplicate-found	"os.open directory os.fsync os.close atomic replacement directory fsync"	src/mindroom/handled_turns.py:531, src/mindroom/interactive.py:201
```

## Findings

### 1. Duplicated homeserver server-name extraction

`src/mindroom/matrix/state.py:149` computes the current Matrix domain by checking `constants.runtime_matrix_server_name(runtime_paths)`, then parsing `constants.runtime_matrix_homeserver(runtime_paths)` by stripping a URL scheme and port.
`src/mindroom/matrix_identifiers.py:75` implements the same fallback behavior in `extract_server_name_from_homeserver`.

The behavior is duplicated, not just related, because both functions encode the same precedence rule and string parsing for Matrix server names.
The only difference to preserve is that `_current_runtime_domain` obtains the homeserver internally from `runtime_paths`, while `extract_server_name_from_homeserver` accepts the homeserver string as an argument.

### 2. Duplicated atomic file replacement and durability logic

`src/mindroom/matrix/state.py:183` writes YAML by creating a temp file in the target directory, dumping content, flushing, `os.fsync`ing the temp file, replacing the destination, fsyncing the directory, and unlinking leftover temp files in `finally`.
`src/mindroom/handled_turns.py:407` performs the same replacement pattern for JSON handled-turn state.
`src/mindroom/interactive.py:189` performs the same replacement pattern for interactive question persistence.
`src/mindroom/tool_system/output_files.py:415` also writes through a temp file, flushes, fsyncs, and replaces the destination, though it writes bytes and does not directory-fsync in the checked snippet.

The duplicated behavior is the persistence mechanism, independent of whether the payload serializer is YAML, JSON, or bytes.
Differences to preserve include cross-process locks in callers, payload serializer, text versus binary mode, optional chmod in output files, and whether directory fsync failures are tolerated.

### 3. Related YAML load-and-validate pattern

`src/mindroom/matrix/state.py:169` uses the standard YAML load pattern `yaml.safe_load(f) or {}`, then validates into a Pydantic model.
`src/mindroom/config/main.py:965` and `src/mindroom/config/main.py:1761` use the same YAML-empty-file handling before validating config data.

This is related, but not a strong duplication candidate on its own.
Matrix state loading also performs account schema migration and writes normalized data back to disk, while config loading resolves runtime paths, validates plugin/tool entries, and logs config counts.

## Proposed Generalization

For server-name extraction, replace `_current_runtime_domain` with a thin call to `matrix_identifiers.extract_server_name_from_homeserver(constants.runtime_matrix_homeserver(runtime_paths), runtime_paths=runtime_paths)`.
That would keep a single source of truth for Matrix domain parsing.

For atomic writes, a small helper could live in a focused persistence module such as `src/mindroom/atomic_io.py`.
The minimal shape would accept a target path plus a writer callback that receives an opened temp file, then handle parent creation, flush, file fsync, replace, optional directory fsync, and temp cleanup.
This should only be introduced if more than one of the existing persistence sites is touched in the same PR; otherwise the current local implementations are understandable.

No refactor recommended for YAML loading alone.

## Risk/tests

Server-name extraction tests should cover `MATRIX_SERVER_NAME`, homeservers with schemes, homeservers without schemes, and homeservers with ports.

Atomic-write refactoring would need tests for successful replacement, temp cleanup after serializer failure, parent-directory creation, file fsync invocation, directory fsync behavior, and preserving caller-specific behavior such as locks and chmod.

Matrix state tests should continue covering cache invalidation, deep-copy mutation isolation, migration of account domains, normalized rewrite behavior, missing-file defaults, room alias resolution, and root-space persistence.
