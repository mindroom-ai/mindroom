## Summary

Top duplication candidate: config-missing/search-location reporting is repeated between `src/mindroom/cli/main.py` and `src/mindroom/cli/config.py`.
The repeated Matrix connection diagnostics in `run` and `avatars sync` are intra-module duplication, not duplicated elsewhere in `./src` except for related doctor reachability checks.
No other meaningful cross-source duplication was found for this primary file.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
version	function	lines 63-66	none-found	version command __version__ Mindroom version	src/mindroom/cli/config.py:52; src/mindroom/cli/doctor.py:1
run	function	lines 70-117	related-only	asyncio.run run command api_port api_host storage_path	src/mindroom/orchestrator.py:1927; src/mindroom/tool_system/tool_hooks.py:301; src/mindroom/api/sandbox_runner.py:1249
_load_active_config_or_exit	function	lines 120-138	related-only	_load_config_quiet CONFIG_LOAD_USER_ERROR_TYPES ensure_writable_config_path config exists	src/mindroom/cli/config.py:524; src/mindroom/cli/config.py:572; src/mindroom/cli/doctor.py:118; src/mindroom/api/config_lifecycle.py:140
_run	async_function	lines 141-189	related-only	make_banner Dashboard API ConnectionError OSError refused bot_main	src/mindroom/orchestrator.py:1927; src/mindroom/cli/doctor.py:597; src/mindroom/avatar_generation.py:441
avatars_generate	function	lines 196-213	related-only	run_avatar_generation AvatarGenerationError _load_active_config_or_exit	src/mindroom/avatar_generation.py:663; src/mindroom/cli/main.py:217
avatars_sync	function	lines 217-242	related-only	set_room_avatars_in_matrix AvatarSyncError ConnectionError OSError refused	src/mindroom/avatar_generation.py:441; src/mindroom/cli/main.py:141
connect	function	lines 246-338	related-only	pair_code complete_local_pairing persist_local_provisioning_env owner placeholders provisioning_url	src/mindroom/cli/connect.py:35; src/mindroom/cli/connect.py:40; src/mindroom/cli/connect.py:99; src/mindroom/matrix/provisioning.py:26
_print_pairing_success_with_exports	function	lines 344-368	related-only	MINDROOM_LOCAL_CLIENT_ID MINDROOM_LOCAL_CLIENT_SECRET MINDROOM_NAMESPACE exports paired successfully	src/mindroom/cli/connect.py:114; src/mindroom/matrix/provisioning.py:30; src/mindroom/cli/config.py:235
_local_client_fingerprint	function	lines 371-376	related-only	fingerprint sha256 hostname config_path non-secret	src/mindroom/cli/connect.py:168; src/mindroom/knowledge/utils.py:189; src/mindroom/knowledge/utils.py:225; src/mindroom/knowledge/refresh_runner.py:754
_print_missing_config_error	function	lines 384-394	duplicate-found	config_search_locations No config found Search locations config init exists not found	src/mindroom/cli/config.py:447; src/mindroom/cli/config.py:528; src/mindroom/cli/config.py:550; src/mindroom/constants.py:270
_print_connection_error	function	lines 397-406	related-only	Could not connect Matrix homeserver runtime_matrix_homeserver unreachable connection refused	src/mindroom/cli/doctor.py:597; src/mindroom/matrix/provisioning.py:101; src/mindroom/avatar_generation.py:453
main	function	lines 409-417	none-found	sys.argv top-level help make_banner app entry point	src/mindroom/knowledge/refresh_runner.py:931; src/mindroom/orchestrator.py:1927
```

## Findings

### Repeated config search-location output

`src/mindroom/cli/main.py:384` prints the missing-config error for `mindroom run`, including quick-start commands and a numbered list from `config_search_locations`.
`src/mindroom/cli/config.py:447` and `src/mindroom/cli/config.py:550` separately format the same search-location list with the same "first match wins" wording and exists/not-found status calculation.
`src/mindroom/cli/config.py:528` is another missing-config branch with a shorter message.

This is functionally duplicated because all call sites present the same config discovery state to the user by enumerating `config_search_locations(process_env)` and mapping `Path.exists()` into Rich status labels.
Differences to preserve: `mindroom run` uses a red fatal "No config.yaml found" onboarding message, `config show` uses a yellow path-specific message, and `config path` is informational and should not exit.

## Proposed Generalization

Add a small CLI config-display helper, likely in `src/mindroom/cli/config.py` or a focused `src/mindroom/cli/output.py`, to render config search locations:

1. Extract a pure-ish helper such as `_print_config_search_locations(process_env, *, heading: str = "Config search locations (first match wins):")`.
2. Use it from `_print_missing_config_error`, `config_show`, and `config_path_cmd`.
3. Keep each command's command-specific preamble and exit behavior unchanged.

No broader refactor is recommended.
The other related logic is already split appropriately: `connect` delegates pairing behavior to `cli/connect.py`, avatar commands delegate to `avatar_generation.py`, and `run` delegates runtime orchestration to `orchestrator.py`.

## Risk/tests

Risk is low if the helper only centralizes display text and status formatting.
Tests should cover missing-config output for `mindroom run`, `mindroom config show`, and `mindroom config path` so command-specific wording and exit codes stay unchanged.
No production code was edited for this audit.
