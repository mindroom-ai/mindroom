## Summary

Top duplication candidates in `src/mindroom/api/config_lifecycle.py`:

1. Snapshot publication logic is duplicated across `config_lifecycle`, `api/main.py`, and `api/runtime_reload.py`.
2. Committed config read helpers repeat the same load-result check, empty-config guard, runtime fallback, and request/app snapshot selection.
3. Mutation and replacement commit helpers share the same optimistic-concurrency commit shape, config-file write, snapshot publish, and HTTP exception mapping.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ConfigLoadResult	class	lines 49-54	related-only	ConfigLoadResult config_load_result load failure result	src/mindroom/api/main.py:20; src/mindroom/api/runtime_reload.py:10
ApiSnapshot	class	lines 58-66	related-only	ApiSnapshot generation runtime_paths config_data auth_state	src/mindroom/api/main.py:258; src/mindroom/api/runtime_reload.py:22; src/mindroom/oauth/registry.py:207; src/mindroom/api/auth.py:334
ApiState	class	lines 70-74	none-found	ApiState config_lock snapshot	require_api_state callers only
MindroomAppState	class	lines 78-85	none-found	MindroomAppState mindroom_app_state app.state	src/mindroom/api/auth.py:345; src/mindroom/api/main.py:252
ensure_app_state	function	lines 88-95	related-only	mindroom_app_state ensure_app_state app.state	src/mindroom/api/main.py:252
app_state	function	lines 98-104	related-only	mindroom_app_state app_state initialized	src/mindroom/api/auth.py:345; src/mindroom/api/main.py:291; src/mindroom/api/runtime_reload.py:59
require_api_state	function	lines 107-113	related-only	require_api_state API context initialized snapshot	src/mindroom/api/auth.py:346; src/mindroom/api/main.py:242; src/mindroom/api/runtime_reload.py:60
_config_error_detail	function	lines 116-127	related-only	iter_config_validation_messages HTTP 422 config errors	src/mindroom/config/main.py:1774
_load_config_result	function	lines 130-157	related-only	load_config authored_model_dump tolerate_plugin_load_errors	src/mindroom/api/runtime_reload.py:90; src/mindroom/config/main.py:1750; src/mindroom/orchestrator.py:1310
load_runtime_config	function	lines 160-173	related-only	load_runtime_config HTTPException load_config tolerate_plugin_load_errors	src/mindroom/api/openai_compat.py:283; src/mindroom/avatar_generation.py:157
raise_for_config_load_result	function	lines 176-183	related-only	config_load_result HTTPException failure cache	src/mindroom/api/runtime_reload.py:99; internal repeated read helpers
_raise_missing_loaded_config	function	lines 186-188	duplicate-found	Failed to load configuration if not snapshot.config_data	src/mindroom/api/config_lifecycle.py:642; src/mindroom/api/config_lifecycle.py:656; src/mindroom/api/config_lifecycle.py:669; src/mindroom/api/config_lifecycle.py:684; src/mindroom/api/config_lifecycle.py:696; src/mindroom/api/config_lifecycle.py:707
_save_config_to_file	function	lines 191-206	related-only	yaml.dump safe_replace config_path tmp sort_keys	src/mindroom/config/main.py:1761; src/mindroom/commands/config_commands.py:201; src/mindroom/knowledge/registry.py:346
_save_raw_config_source_to_file	function	lines 209-217	related-only	write_text source safe_replace config_path tmp	src/mindroom/oauth/state.py:106; src/mindroom/matrix/invited_rooms_store.py:54
persist_runtime_validated_config	function	lines 220-247	related-only	authored_model_dump persist_runtime_validated_config registered_api_states	src/mindroom/commands/config_commands.py:338; src/mindroom/custom_tools/config_manager.py:86
_validated_config_payload	function	lines 250-256	related-only	validate_with_runtime authored_model_dump	src/mindroom/custom_tools/config_manager.py:85; src/mindroom/config/main.py:950
register_api_app	function	lines 259-262	none-found	REGISTERED_API_APPS register_api_app weakset	src/mindroom/api/main.py:267
_registered_api_states	function	lines 265-275	none-found	REGISTERED_API_APPS require_api_state TypeError	continue only in this module
request_snapshot	function	lines 278-281	duplicate-found	request.scope api_snapshot isinstance ApiSnapshot	src/mindroom/api/auth.py:336; src/mindroom/api/auth.py:368
store_request_snapshot	function	lines 284-287	duplicate-found	request.scope api_snapshot store snapshot	src/mindroom/api/auth.py:363
bind_current_request_snapshot	function	lines 290-297	duplicate-found	bind request snapshot config_lock current snapshot	src/mindroom/api/auth.py:334
_request_or_current_snapshot	function	lines 300-305	duplicate-found	request-bound snapshot else app snapshot	src/mindroom/api/auth.py:366
_published_snapshot	function	lines 308-331	duplicate-found	published_snapshot generation replace ApiSnapshot	src/mindroom/api/main.py:210; src/mindroom/api/runtime_reload.py:22
_stale_snapshot_error	function	lines 334-339	duplicate-found	409 Configuration changed while request was in progress	src/mindroom/api/runtime_reload.py:67
api_runtime_paths	function	lines 342-344	related-only	api_runtime_paths committed runtime paths	src/mindroom/api/main.py:205; src/mindroom/api/openai_compat.py:289
committed_generation	function	lines 347-349	none-found	committed_generation snapshot.generation	src/mindroom/api/main.py:503; src/mindroom/api/main.py:535
_raise_if_generation_mismatch	function	lines 352-357	duplicate-found	expected_generation snapshot.generation stale_snapshot_error	src/mindroom/api/config_lifecycle.py:389; src/mindroom/api/config_lifecycle.py:446; src/mindroom/api/config_lifecycle.py:472; src/mindroom/api/runtime_reload.py:63
_build_mutated_config	function	lines 360-372	duplicate-found	deepcopy config_data mutate validate payload	src/mindroom/api/config_lifecycle.py:523; src/mindroom/api/config_lifecycle.py:559
_commit_mutated_snapshot	function	lines 375-399	duplicate-found	config_lock current.generation runtime_paths save published_snapshot	src/mindroom/api/config_lifecycle.py:433; src/mindroom/api/config_lifecycle.py:458; src/mindroom/api/runtime_reload.py:61
_validate_replacement_payload	function	lines 402-407	related-only	validate replacement payload validate_with_runtime	src/mindroom/api/config_lifecycle.py:250
_validate_raw_config_source	function	lines 410-430	related-only	tempfile validation config_path load_config authored_model_dump	src/mindroom/api/sandbox_runner.py:156; src/mindroom/config/main.py:1750
_commit_replaced_snapshot	function	lines 433-455	duplicate-found	commit replacement snapshot generation runtime_paths save published	src/mindroom/api/config_lifecycle.py:375; src/mindroom/api/config_lifecycle.py:458
_commit_raw_replaced_snapshot	function	lines 458-481	duplicate-found	commit raw replacement generation runtime_paths save published	src/mindroom/api/config_lifecycle.py:375; src/mindroom/api/config_lifecycle.py:433
_build_and_commit_mutation	function	lines 484-520	duplicate-found	initial snapshot build commit HTTPException ValidationError error_prefix	src/mindroom/api/config_lifecycle.py:523; src/mindroom/api/config_lifecycle.py:559
_build_and_commit_replacement	function	lines 523-556	duplicate-found	initial snapshot validate commit HTTPException error_prefix	src/mindroom/api/config_lifecycle.py:484; src/mindroom/api/config_lifecycle.py:559
_build_and_commit_raw_replacement	function	lines 559-591	duplicate-found	initial snapshot validate raw commit HTTPException error_prefix	src/mindroom/api/config_lifecycle.py:484; src/mindroom/api/config_lifecycle.py:523
load_config_from_file	function	lines 594-606	related-only	load_config_result update config_data lock	src/mindroom/api/config_lifecycle.py:609
load_config_into_app	function	lines 609-630	related-only	load_config_result publish snapshot config_lock	src/mindroom/api/runtime_reload.py:90; src/mindroom/api/main.py:351
read_app_committed_config	function	lines 633-644	duplicate-found	read committed config raise_for_config_load_result missing loaded reader	src/mindroom/api/config_lifecycle.py:647; src/mindroom/api/config_lifecycle.py:677
read_app_committed_config_and_runtime	function	lines 647-658	duplicate-found	read committed config runtime raise_for_config_load_result missing loaded reader	src/mindroom/api/config_lifecycle.py:633; src/mindroom/api/config_lifecycle.py:689
read_app_committed_runtime_config	function	lines 661-674	duplicate-found	read committed runtime Config.model_validate fallback	src/mindroom/api/config_lifecycle.py:701; src/mindroom/oauth/registry.py:219
read_committed_config	function	lines 677-686	duplicate-found	request read committed config raise_for_config_load_result missing loaded reader	src/mindroom/api/config_lifecycle.py:633; src/mindroom/api/config_lifecycle.py:689
read_committed_config_and_runtime	function	lines 689-698	duplicate-found	request read committed config runtime raise_for_config_load_result missing loaded reader	src/mindroom/api/config_lifecycle.py:647; src/mindroom/api/matrix_operations.py:136
read_committed_runtime_config	function	lines 701-712	duplicate-found	request read committed runtime Config.model_validate fallback	src/mindroom/api/config_lifecycle.py:661; src/mindroom/oauth/registry.py:219
write_committed_config	function	lines 715-727	related-only	request write committed config initial_snapshot	src/mindroom/api/config_lifecycle.py:730
write_app_committed_config	function	lines 730-737	related-only	app write committed config build_and_commit_mutation	src/mindroom/api/config_lifecycle.py:715
replace_committed_config	function	lines 740-754	related-only	request replace committed config expected_generation	src/mindroom/api/config_lifecycle.py:757; src/mindroom/api/main.py:518
replace_app_committed_config	function	lines 757-764	related-only	app replace committed config	src/mindroom/api/config_lifecycle.py:740
read_raw_config_source	function	lines 767-775	related-only	read_text UnicodeDecodeError errors replace	src/mindroom/custom_tools/coding.py:544; src/mindroom/custom_tools/coding.py:1018
replace_raw_config_source	function	lines 778-792	related-only	replace raw config source expected_generation	src/mindroom/api/main.py:550
watch_config	async_function	lines 795-808	duplicate-found	watch config file on_config_change watch_file	src/mindroom/orchestrator.py:1699; src/mindroom/api/main.py:298
watch_config.<locals>._handle_config_change	nested_async_function	lines 804-806	duplicate-found	Config file changed on_config_change callback	src/mindroom/orchestrator.py:1693; src/mindroom/api/main.py:337
```

## Findings

### 1. Snapshot publication is repeated in three API modules

`src/mindroom/api/config_lifecycle.py:308` builds a new `ApiSnapshot` by carrying forward unchanged fields, applying optional replacements, and incrementing generation.
`src/mindroom/api/main.py:210` and `src/mindroom/api/runtime_reload.py:22` repeat the same behavior with small differences.
`api/main.py` additionally supports `increment_generation`, `runtime_paths`, and `auth_state`.
`runtime_reload.py` supports `runtime_paths` and `auth_state`.
`config_lifecycle.py` only supports config fields and uses `dataclasses.replace`.

This is functional duplication because all three call sites implement the same snapshot-copy/publish operation and must preserve the same semantics for config data, runtime config, config load result, auth state, runtime paths, and generation.

### 2. Request-bound snapshot helpers overlap with authenticated request snapshot binding

`src/mindroom/api/config_lifecycle.py:278`, `src/mindroom/api/config_lifecycle.py:284`, `src/mindroom/api/config_lifecycle.py:290`, and `src/mindroom/api/config_lifecycle.py:300` implement request-scope snapshot retrieval, storage, and current-snapshot binding.
`src/mindroom/api/auth.py:334` performs a similar bind operation for authenticated requests, including request-scope reuse, app state lookup, config lock acquisition, current snapshot access, and request-scope storage at `src/mindroom/api/auth.py:363`.

The behavior is not identical because auth binding may create or refresh `ApiAuthState` before storing the snapshot.
The duplicated core is the request-scope snapshot protocol and the locked current-snapshot capture.

### 3. Committed config read helpers repeat the same guard and projection flow

`src/mindroom/api/config_lifecycle.py:633`, `src/mindroom/api/config_lifecycle.py:647`, `src/mindroom/api/config_lifecycle.py:661`, `src/mindroom/api/config_lifecycle.py:677`, `src/mindroom/api/config_lifecycle.py:689`, and `src/mindroom/api/config_lifecycle.py:701` all perform variants of:

- select an app or request snapshot,
- call `raise_for_config_load_result`,
- reject empty config data via `_raise_missing_loaded_config`,
- return a projection, runtime paths, or runtime config.

The app variants acquire `config_lock`; the request variants rely on a request-bound or current snapshot.
The runtime config variants also duplicate `Config.model_validate(snapshot.config_data, context={"runtime_paths": runtime_paths})`.
`src/mindroom/oauth/registry.py:219` repeats the same runtime-config-or-model-validate fallback for snapshots.

### 4. Commit helpers share optimistic-concurrency and publish mechanics

`src/mindroom/api/config_lifecycle.py:375`, `src/mindroom/api/config_lifecycle.py:433`, and `src/mindroom/api/config_lifecycle.py:458` all:

- lock the initial API state,
- reload current API state,
- compare generation and runtime paths,
- write config data or raw source,
- publish a new snapshot with validated payload, validated runtime config, and successful load result,
- return either the mutation result or the new generation.

The differences to preserve are the writer function (`_save_config_to_file` vs `_save_raw_config_source_to_file`), whether to call `raise_for_config_load_result` on stale mutation writes, and the return value.

### 5. Build-and-commit wrappers duplicate snapshot selection and exception mapping

`src/mindroom/api/config_lifecycle.py:484`, `src/mindroom/api/config_lifecycle.py:523`, and `src/mindroom/api/config_lifecycle.py:559` all:

- require API state,
- use either an initial snapshot or the current locked snapshot,
- validate/build off-lock,
- commit under optimistic concurrency,
- pass through `HTTPException`,
- map validation or load errors to HTTP 422,
- map all other exceptions to HTTP 500 with an operation-specific prefix.

The raw YAML path uses `CONFIG_LOAD_USER_ERROR_TYPES` and `_config_error_detail`, while dict mutation/replacement paths catch `ValidationError` and `ConfigRuntimeValidationError` separately.

### 6. Config-file watch callbacks are near-duplicates

`src/mindroom/api/config_lifecycle.py:795` wraps `watch_file` for the API cache.
`src/mindroom/orchestrator.py:1699` wraps the same watcher for orchestrator hot reload.
Both nested callbacks log that the config changed and invoke a supplied reload action.

`src/mindroom/api/main.py:298` is related but not directly interchangeable because it polls and automatically rebinds when runtime paths change instead of using `watch_file`.

## Proposed Generalization

1. Move the richer snapshot publisher shape into `config_lifecycle._published_snapshot`, adding optional `runtime_paths`, `auth_state`, and `increment_generation` parameters, then remove the local `_published_snapshot` copies from `api/main.py` and `api/runtime_reload.py`.
2. Add a small private helper such as `_validated_snapshot_config(snapshot) -> tuple[dict[str, Any], constants.RuntimePaths]` or `_read_valid_snapshot(snapshot, reader)` to centralize the load-result and empty-config guards.
3. Add a private helper for `snapshot.runtime_config or Config.model_validate(...)`, and use it from both committed runtime config readers and OAuth provider loading.
4. Consider one private commit helper that accepts a writer callback and return projection for the three commit paths.
5. Leave the watcher wrappers alone unless snapshot publication is already being touched; the duplication is small and the API main watcher has different runtime-path rebinding behavior.

## Risk/tests

Snapshot publication is the highest-risk refactor because generation increments are part of the API optimistic-concurrency contract.
Tests should cover config save, raw config save, runtime reload, auth snapshot binding, and response generation headers.

Committed read helper consolidation should cover invalid cached config, missing config data, request-bound snapshots, app-only reads, and runtime config fallback.

Commit helper consolidation should cover stale generation, stale runtime paths, validation errors, raw YAML validation errors, successful dict replacement, successful raw replacement, and mutation return values.

No production code was edited for this audit.
