## Summary

Top duplication candidates:

1. `.env` read/upsert/write behavior in `src/mindroom/cli/local_stack.py` duplicates local provisioning persistence in `src/mindroom/cli/connect.py`.
2. Matrix homeserver `/versions` probing overlaps with `src/mindroom/orchestration/runtime.py` and `src/mindroom/cli/doctor.py`, with different sync/async and CLI/reporting needs.
3. Small process/binary helpers are related to several source call sites, but the local-stack variants are narrow enough that a shared abstraction is not clearly worth introducing.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
local_stack_setup	function	lines 31-112	related-only	local stack setup docker cinny synapse MATRIX_HOMESERVER	src/mindroom/cli/main.py:250; src/mindroom/cli/connect.py:99; src/mindroom/orchestration/runtime.py:347
_infer_server_name	function	lines 115-121	related-only	extract server name homeserver urlparse MATRIX_SERVER_NAME	src/mindroom/matrix_identifiers.py:75; src/mindroom/entity_resolution.py:45; src/mindroom/avatar_generation.py:340
_write_local_cinny_config	function	lines 124-147	none-found	cinny config config.json homeserverList featuredCommunities hashRouter	none
_persist_local_matrix_env	function	lines 150-170	duplicate-found	persist .env read splitlines upsert MATRIX_HOMESERVER MATRIX_SERVER_NAME	src/mindroom/cli/connect.py:99; src/mindroom/cli/connect.py:190; src/mindroom/cli/config.py:166
_upsert_env_var	function	lines 173-181	duplicate-found	upsert env var export regex KEY=value preserving lines	src/mindroom/cli/connect.py:190; src/mindroom/cli/connect.py:122
_require_supported_platform	function	lines 184-189	related-only	platform system linux darwin os requirements	src/mindroom/tool_system/skills.py:541
_require_binary	function	lines 192-197	related-only	shutil.which required binary missing bins	src/mindroom/tool_system/skills.py:561; src/mindroom/frontend_assets.py:57; src/mindroom/tool_system/dependencies.py:163
_start_synapse_stack	function	lines 200-211	related-only	docker compose up subprocess run compose file	src/mindroom/tool_system/dependencies.py:188; src/mindroom/knowledge/manager.py:235
_start_cinny_container	function	lines 214-241	related-only	docker rm run container subprocess failure	src/mindroom/tool_system/dependencies.py:188; src/mindroom/tool_system/dependencies.py:198
_wait_for_service	function	lines 244-250	related-only	wait for service http success typer exit	src/mindroom/cli/doctor.py:161; src/mindroom/cli/local_stack.py:324
_wait_for_matrix_homeserver	function	lines 253-265	duplicate-found	wait Matrix homeserver versions response_has_matrix_versions	src/mindroom/orchestration/runtime.py:347; src/mindroom/cli/doctor.py:597; src/mindroom/matrix/health.py:53
_print_local_stack_summary	function	lines 268-299	none-found	local stack summary stop commands persist env	none
_run_command	function	lines 302-315	related-only	subprocess.run capture_output text cwd check CompletedProcess	src/mindroom/tool_system/dependencies.py:188; src/mindroom/knowledge/manager.py:235; src/mindroom/custom_tools/coding.py:390
_print_command_failure	function	lines 318-321	related-only	stderr strip stdout strip no error details subprocess failure	src/mindroom/tool_system/dependencies.py:188; src/mindroom/custom_tools/coding.py:887
_wait_for_http_success	function	lines 324-342	related-only	httpx.get retry timeout response matcher sleep	src/mindroom/cli/doctor.py:161; src/mindroom/orchestration/runtime.py:347
```

## Findings

### 1. Duplicate `.env` persistence/upsert logic

`src/mindroom/cli/local_stack.py:150` resolves the config-adjacent `.env`, creates the directory, reads existing lines, applies key/value updates with `_upsert_env_var`, and writes the file back with a trailing newline.
`src/mindroom/cli/connect.py:99` performs the same flow for local provisioning credentials.
The helper `_upsert_env_var` is literally duplicated at `src/mindroom/cli/local_stack.py:173` and `src/mindroom/cli/connect.py:190`, including the `export`-tolerant regex and replacement behavior.

Differences to preserve:

- `local_stack` writes `MATRIX_HOMESERVER`, `MATRIX_SSL_VERIFY=false`, and `MATRIX_SERVER_NAME`.
- `connect` strips the provisioning URL, includes client credentials and namespace, and conditionally writes `OWNER_MATRIX_USER_ID_ENV` only after parsing.
- Both currently rewrite matching `export KEY=...` lines as plain `KEY=...`.

### 2. Duplicate Matrix `/versions` readiness/checking behavior

`src/mindroom/cli/local_stack.py:253` builds a Matrix `/versions` URL, polls it for up to 60 seconds, and accepts only `response_has_matrix_versions`.
`src/mindroom/orchestration/runtime.py:347` performs the same Matrix-specific readiness loop asynchronously for runtime startup.
`src/mindroom/cli/doctor.py:597` performs a one-shot version of the same Matrix check for diagnostics.

Differences to preserve:

- `local_stack` is synchronous, hard-codes `verify=False`, emits CLI text, and exits with `typer.Exit`.
- runtime startup is asynchronous, uses runtime-configured SSL verification, logs retry details, supports configurable timeout/interval, and raises `TimeoutError`.
- doctor is a one-shot status check returning pass/fail/warning counts.

### 3. Related-only command and prerequisite helpers

`_require_binary`, `_run_command`, and `_print_command_failure` overlap with broader process patterns in `src/mindroom/tool_system/dependencies.py`, `src/mindroom/frontend_assets.py`, `src/mindroom/knowledge/manager.py`, and `src/mindroom/custom_tools/coding.py`.
These are functionally related but not clear duplication: each call site has different capture, environment, timeout, stdout/stderr, and failure-reporting requirements.
No shared helper is recommended from this file alone.

## Proposed Generalization

1. Add a small CLI env-file helper, for example `src/mindroom/cli/env_file.py`, with `upsert_env_vars(env_path: Path, updates: Mapping[str, str]) -> Path` and a private `upsert_env_var` equivalent.
2. Update `persist_local_provisioning_env` and `_persist_local_matrix_env` to build their update dictionaries locally and delegate only the read/upsert/write mechanics.
3. Keep Matrix readiness separate for now unless more synchronous polling call sites appear; the existing async runtime loop and CLI exit behavior differ enough that unifying them would add parameters without much payoff.

## Risk/tests

Primary risk for the env-file helper is changing exact file rewrite behavior, especially preserving unrelated lines, matching `export KEY=...`, and keeping a final newline.
Focused tests should cover both provisioning and local-stack env persistence after extracting the shared helper.

Matrix readiness should keep existing coverage around `matrix_versions_url` and `response_has_matrix_versions`.
If a shared poll helper is later introduced, tests need separate cases for sync CLI timeout, async runtime timeout, SSL verification source, and invalid `/versions` payloads.
