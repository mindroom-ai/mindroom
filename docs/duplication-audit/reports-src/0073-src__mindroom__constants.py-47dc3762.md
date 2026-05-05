## Summary

Top duplication candidates in `src/mindroom/constants.py`:

1. Boolean environment parsing is duplicated between `RuntimePaths.env_flag` / `runtime_env_flag` and Kubernetes worker config parsing.
2. Provider-to-env-key behavior is centralized for most callers, but CLI config still keeps a provider preset env-key map with overlapping values.
3. Atomic temp-file replacement behavior exists both as `safe_replace` and as several local `tmp_path.replace(...)` persistence flows, though most local flows have stricter fsync or temp-file requirements.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
is_workspace_env_overlay_name_allowed	function	lines 184-194	none-found	workspace env overlay runner control MINDROOM_SANDBOX	none
RuntimePaths	class	lines 198-239	related-only	RuntimePaths config_path storage_root env_file_values MappingProxyType	src/mindroom/api/sandbox_exec.py:202; src/mindroom/api/sandbox_runner.py:120; src/mindroom/workers/backends/kubernetes_resources.py:883
RuntimePaths.env_value	method	lines 222-232	related-only	env_value runtime_paths MINDROOM_CONFIG_PATH MINDROOM_STORAGE_PATH	src/mindroom/credentials_sync.py:50; src/mindroom/matrix/provisioning.py:16; src/mindroom/oauth/providers.py:126
RuntimePaths.env_flag	method	lines 234-239	duplicate-found	env_flag strip lower true yes on _read_bool_env	src/mindroom/workers/backends/kubernetes_config.py:85; src/mindroom/constants.py:787
_copy_process_env	function	lines 242-245	related-only	dict os.environ process_env copy	src/mindroom/api/sandbox_exec.py:233; src/mindroom/tool_system/dependencies.py:147; src/mindroom/knowledge/manager.py:1045
_runtime_env_file_values_for_path	function	lines 248-252	related-only	dotenv_values env_path .env string values	src/mindroom/cli/config.py:194
_resolve_runtime_relative_path	function	lines 255-260	related-only	Path expanduser absolute base_dir resolve	src/mindroom/constants.py:849; src/mindroom/codex_model.py:107
_configured_config_path	function	lines 263-267	none-found	MINDROOM_CONFIG_PATH strip configured config path	none
config_search_locations	function	lines 270-286	related-only	config_search_locations config discovery search paths	src/mindroom/cli/main.py:391; src/mindroom/cli/config.py:451; src/mindroom/cli/config.py:562
_storage_root_from_env_values	function	lines 289-293	none-found	MINDROOM_STORAGE_PATH env_file_values config_dir	none
_storage_root_from_env_path	function	lines 296-298	none-found	storage_root_from_env_path dotenv MINDROOM_STORAGE_PATH	none
resolve_runtime_paths	function	lines 301-339	related-only	resolve_runtime_paths RuntimePaths config storage .env	src/mindroom/runtime_support.py:133; src/mindroom/runtime_support.py:202
_with_primary_runtime_env	function	lines 342-356	related-only	MINDROOM_CONFIG_PATH MINDROOM_STORAGE_PATH normalized process_env	src/mindroom/runtime_support.py:133; src/mindroom/workers/backends/kubernetes_resources.py:872
resolve_primary_runtime_paths	function	lines 359-372	related-only	resolve_primary_runtime_paths config_path storage_path	src/mindroom/constants.py:963
serialize_runtime_paths	function	lines 375-382	related-only	serialize RuntimePaths config_path storage_root process_env env_file_values	src/mindroom/workers/backends/kubernetes_resources.py:866
_is_public_runtime_startup_env_name	function	lines 385-392	related-only	public runtime startup env secret suffix excluded database	src/mindroom/constants.py:395; src/mindroom/constants.py:405
_is_isolated_runtime_public_env_name	function	lines 395-402	related-only	isolated runtime public env secret suffix excluded database	src/mindroom/constants.py:385; src/mindroom/constants.py:405
_is_sandbox_execution_runtime_env_name	function	lines 405-412	related-only	sandbox execution runtime env secret suffix excluded database	src/mindroom/constants.py:385; src/mindroom/constants.py:395
serialize_public_runtime_paths	function	lines 415-428	related-only	serialize public runtime paths filter env	src/mindroom/constants.py:375; src/mindroom/constants.py:431
serialize_startup_manifest	function	lines 431-443	related-only	startup manifest runtime_paths tool_validation_snapshot	src/mindroom/api/sandbox_runner.py:88; src/mindroom/workers/backends/kubernetes_resources.py:829
startup_manifest_json	function	lines 446-461	related-only	json dumps separators sort_keys startup manifest	src/mindroom/api/oauth.py:298
startup_manifest_sha256	function	lines 464-476	none-found	sha256 startup manifest json	none
sandbox_startup_manifest_path	function	lines 479-481	related-only	startup_manifest_path storage_root .runtime startup_manifest	src/mindroom/api/sandbox_runner.py:80; src/mindroom/workers/backends/kubernetes_resources.py:846
write_startup_manifest	function	lines 484-502	related-only	write startup_manifest mkdir write_text	src/mindroom/workers/backends/kubernetes_resources.py:829
_is_json_object	function	lines 505-506	related-only	isinstance value dict TypeGuard JSON object	src/mindroom/api/sandbox_runner.py:89
deserialize_runtime_paths	function	lines 509-545	related-only	deserialize RuntimePaths payload config_path storage_root process_env	src/mindroom/api/sandbox_runner.py:98
deserialize_startup_manifest	function	lines 548-557	related-only	deserialize startup manifest runtime_paths tool_validation_snapshot	src/mindroom/api/sandbox_runner.py:98; src/mindroom/api/sandbox_runner.py:138
_expand_runtime_path_vars	function	lines 560-575	related-only	placeholder regex MINDROOM_CONFIG_PATH MINDROOM_STORAGE_PATH replace	src/mindroom/mcp/transports.py:40
_expand_runtime_path_vars.<locals>._replace	nested_function	lines 563-573	related-only	re.Match replace placeholder env_value	src/mindroom/mcp/transports.py:40; src/mindroom/voice_handler.py:533
exported_process_env	function	lines 578-580	related-only	exported process env os.environ copy	src/mindroom/constants.py:242
runtime_env_values	function	lines 583-589	related-only	merge env_file_values process_env MINDROOM_CONFIG_PATH MINDROOM_STORAGE_PATH	src/mindroom/constants.py:701; src/mindroom/workers/backends/kubernetes_config.py:145
_is_known_worker_credential_env_name	function	lines 592-597	related-only	known worker credential env GOOGLE_APPLICATION_CREDENTIALS GITHUB_TOKEN VERTEXAI	src/mindroom/constants.py:605
is_runtime_database_url_env_name	function	lines 600-602	related-only	DATABASE_URL suffix database url env	src/mindroom/constants.py:385; src/mindroom/constants.py:613
_is_execution_runtime_process_env_name	function	lines 605-610	related-only	execution runtime process env public known credential	src/mindroom/constants.py:619
_is_allowed_execution_runtime_env_file_name	function	lines 613-616	related-only	allowed execution runtime env file database excluded	src/mindroom/constants.py:619
_execution_runtime_env_layers	function	lines 619-630	related-only	execution runtime env layers process env_file filters	src/mindroom/constants.py:633; src/mindroom/constants.py:647
_sandbox_execution_runtime_env_layers	function	lines 633-644	related-only	sandbox execution runtime env layers filter	src/mindroom/constants.py:619; src/mindroom/constants.py:647
_isolated_runtime_env_layers	function	lines 647-656	related-only	isolated runtime env layers filter	src/mindroom/constants.py:619; src/mindroom/constants.py:633
_shell_extra_env_patterns	function	lines 659-662	related-only	split whitespace comma extra env passthrough patterns	src/mindroom/mcp/toolkit.py:35; src/mindroom/tool_system/metadata.py:1178
shell_extra_env_values	function	lines 665-686	related-only	fnmatch extra env passthrough runner control MINDROOM_SANDBOX	src/mindroom/authorization.py:142; src/mindroom/tool_approval.py:173
sandbox_shell_system_env_values	function	lines 689-698	related-only	system env allowlist subprocess passthrough	src/mindroom/api/sandbox_exec.py:233; src/mindroom/api/sandbox_exec.py:238
execution_runtime_env_values	function	lines 701-716	related-only	execution runtime env values merge env layers	src/mindroom/api/sandbox_exec.py:196; src/mindroom/tool_system/sandbox_proxy.py:453
sandbox_execution_runtime_env_values	function	lines 719-726	related-only	sandbox execution runtime env values merge env layers	src/mindroom/api/sandbox_exec.py:182; src/mindroom/tool_system/sandbox_proxy.py:453
isolated_runtime_paths	function	lines 729-739	related-only	isolated RuntimePaths filtered process env env_file_values	src/mindroom/api/sandbox_runner.py:120; src/mindroom/workers/backends/kubernetes_resources.py:883
shell_execution_runtime_env_values	function	lines 742-756	related-only	shell execution runtime env extra passthrough	src/mindroom/tools/shell.py:364
sandbox_shell_execution_runtime_env_values	function	lines 759-773	related-only	sandbox shell execution env system extra passthrough	src/mindroom/api/sandbox_exec.py:174; src/mindroom/tool_system/sandbox_proxy.py:447
runtime_env_path	function	lines 776-784	related-only	runtime env path resolve relative config_dir	src/mindroom/credentials_sync.py:54; src/mindroom/constants.py:1117
runtime_env_flag	function	lines 787-797	duplicate-found	runtime_env_flag strip lower true yes on _read_bool_env	src/mindroom/workers/backends/kubernetes_config.py:85; src/mindroom/constants.py:234
runtime_matrix_homeserver	function	lines 800-802	related-only	MATRIX_HOMESERVER default localhost 8008	src/mindroom/cli/local_stack.py:162; src/mindroom/config/matrix.py:268
runtime_matrix_ssl_verify	function	lines 805-807	related-only	MATRIX_SSL_VERIFY runtime flag false local stack	src/mindroom/cli/local_stack.py:163; src/mindroom/cli/local_stack.py:295
runtime_matrix_server_name	function	lines 810-812	related-only	MATRIX_SERVER_NAME env value	src/mindroom/cli/config.py:86
runtime_mindroom_namespace	function	lines 815-821	related-only	MINDROOM_NAMESPACE strip lower namespace	src/mindroom/matrix_identifiers.py:21; src/mindroom/cli/connect.py:118
matrix_state_file	function	lines 824-826	related-only	matrix_state.yaml storage_root	src/mindroom/matrix/room_cleanup.py:38; src/mindroom/matrix/users.py:64
tracking_dir	function	lines 829-831	none-found	storage_root tracking directory	none
memory_dir	function	lines 834-836	none-found	storage_root memory directory	none
credentials_dir	function	lines 839-841	related-only	storage_root credentials directory	src/mindroom/credentials.py:237
encryption_keys_dir	function	lines 844-846	none-found	storage_root encryption_keys directory	none
resolve_config_relative_path	function	lines 849-861	related-only	config relative path placeholders resolve	src/mindroom/knowledge/manager.py:510; src/mindroom/mcp/transports.py:40
resolve_config_relative_path_preserving_leaf	function	lines 864-872	related-only	config relative path preserving leaf no resolve	src/mindroom/api/config_lifecycle.py:425
_docker_container_enabled	function	lines 875-877	none-found	DOCKER_CONTAINER env_flag	none
_use_storage_path_for_workspace_assets	function	lines 880-891	none-found	workspace assets storage path docker config storage	none
avatars_dir	function	lines 894-905	related-only	avatars directory config_dir storage_root	src/mindroom/avatar_generation.py:441; src/mindroom/cli/main.py:195
bundled_avatars_dir	function	lines 908-910	none-found	bundled avatars Path __file__ parents	none
workspace_avatar_path	function	lines 913-919	related-only	workspace avatar path entity_type entity_name png	src/mindroom/avatar_generation.py:199
resolve_avatar_path	function	lines 922-946	related-only	resolve avatar workspace bundled fallback	src/mindroom/avatar_generation.py:199; src/mindroom/matrix/rooms.py:261
find_config	function	lines 949-960	related-only	find config MINDROOM_CONFIG_PATH config.yaml home	src/mindroom/cli/config.py:261; src/mindroom/cli/main.py:391
set_runtime_storage_path	function	lines 963-970	none-found	set runtime storage path resolve primary	none
env_key_for_provider	function	lines 1025-1032	duplicate-found	provider env key gemini google API key required env	src/mindroom/cli/config.py:78; src/mindroom/credentials_sync.py:382; src/mindroom/cli/doctor.py:268
patch_chromadb_for_python314	function	lines 1035-1085	none-found	chromadb pydantic python 3.14 inspect_namespace	none
patch_chromadb_for_python314.<locals>._patched_inspect_namespace	nested_function	lines 1061-1082	none-found	non-annotated attribute chroma_coordinator_host inspect_namespace	none
safe_replace	function	lines 1088-1099	duplicate-found	tmp_path replace target fallback copy2 unlink atomic replace	src/mindroom/config/main.py:1746; src/mindroom/api/config_lifecycle.py:206; src/mindroom/matrix/invited_rooms_store.py:58; src/mindroom/oauth/state.py:107; src/mindroom/memory/auto_flush.py:235
ensure_writable_config_path	function	lines 1102-1130	related-only	MINDROOM_CONFIG_TEMPLATE create minimal config chmod 0600	src/mindroom/cli/config.py:372; src/mindroom/codex_model.py:145
```

## Findings

### 1. Boolean env parsing is duplicated

`RuntimePaths.env_flag` and `runtime_env_flag` both parse boolean env values with the same accepted truthy set: `"1"`, `"true"`, `"yes"`, and `"on"` at `src/mindroom/constants.py:234` and `src/mindroom/constants.py:787`.
`src/mindroom/workers/backends/kubernetes_config.py:85` repeats the same behavior in `_read_bool_env`.

Why this is duplicated: each function reads a possibly absent env value, preserves a default for missing values, and normalizes the same truthy strings with `strip().lower()`.

Difference to preserve: `_read_bool_env` reads from an arbitrary `Mapping[str, str]`, while the constants helpers read through a `RuntimePaths` context.

### 2. Provider env-key mapping is mostly centralized but still partly repeated in CLI config

`env_key_for_provider` and `PROVIDER_ENV_KEYS` centralize provider API-key env names and the `gemini` to `google` alias at `src/mindroom/constants.py:1007` and `src/mindroom/constants.py:1025`.
Several call sites already use this central source, including doctor checks at `src/mindroom/cli/doctor.py:426`, memory LLM checks at `src/mindroom/cli/doctor.py:513`, embedder checks at `src/mindroom/cli/doctor.py:555`, and credential syncing via the reverse map at `src/mindroom/credentials_sync.py:34`.

`src/mindroom/cli/config.py:78` still maintains `_REQUIRED_ENV_KEYS` with overlapping values for `anthropic`, `openai`, and `openrouter`, and starter env rendering repeats those key names at `src/mindroom/cli/config.py:1008`.

Why this is duplicated: CLI config repeats provider-to-env-key knowledge that otherwise lives in `constants.py`.

Difference to preserve: `codex` and `vertexai_claude` are config-init presets, not normal `PROVIDER_ENV_KEYS` entries, and `vertexai_claude` uses `VERTEXAI_CLAUDE_ENV_KEYS` rather than one API-key variable.

### 3. Atomic replacement behavior overlaps with local persistence writers

`safe_replace` wraps `Path.replace` and falls back to `shutil.copy2` plus temp cleanup for bind-mount failures at `src/mindroom/constants.py:1088`.
Several production writers either use it directly (`src/mindroom/config/main.py:1746`, `src/mindroom/api/config_lifecycle.py:206`, `src/mindroom/api/skills.py:97`, `src/mindroom/matrix/invited_rooms_store.py:58`) or perform local `tmp_path.replace(...)` persistence flows (`src/mindroom/oauth/state.py:107`, `src/mindroom/memory/auto_flush.py:235`, `src/mindroom/matrix/state.py:200`).

Why this is duplicated: the local flows share the core behavior of writing a temp file and replacing the target.

Difference to preserve: `src/mindroom/matrix/state.py:183` fsyncs the temp file and containing directory, so replacing it with `safe_replace` directly would lose durability behavior.
Some local flows intentionally use very small JSON state files and may not need the bind-mount fallback.

## Proposed Generalization

1. Add a tiny `parse_env_bool(raw: str | None, *, default: bool = False) -> bool` helper in `constants.py`, then call it from `RuntimePaths.env_flag`, `runtime_env_flag`, and `_read_bool_env`.
2. In CLI config, derive `_REQUIRED_ENV_KEYS` for ordinary provider presets from `env_key_for_provider`, while keeping explicit special cases for `codex` and `vertexai_claude`.
3. Leave `safe_replace` consolidation conservative: only migrate local `tmp_path.replace(...)` call sites that do not require fsync semantics and that benefit from bind-mount tolerance.

## Risk/tests

Boolean parsing is low risk but should be covered by existing constants and Kubernetes backend config tests, especially missing-value defaults and accepted truthy spellings.
Provider env-key deduplication needs CLI config-init tests for all presets, especially `codex`, `openrouter`, and `vertexai_claude`.
Safe replacement changes should be tested at the persistence helper level and should not touch Matrix state fsync behavior without explicit durability tests.
