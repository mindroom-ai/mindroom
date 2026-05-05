Summary: The strongest duplication candidates in `src/mindroom/cli/config.py` are the ad hoc `.env` file mutation flow, repeated config search-location rendering, and config validation/error formatting that overlaps with existing shared config error helpers.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_config_init_storage_plan	function	lines 103-115	related-only	resolve_runtime_paths MINDROOM_STORAGE_PATH storage_root env_file_values	src/mindroom/constants.py:289, src/mindroom/constants.py:301
_config_init_owner_user_id	function	lines 118-121	related-only	OWNER_MATRIX_USER_ID parse_owner_matrix_user_id env_value	src/mindroom/cli/connect.py:120, src/mindroom/cli/connect.py:129
_default_mind_workspace	function	lines 124-126	none-found	agent_workspace_root_path storage_root mind workspace	none
_path_string_for_config	function	lines 129-136	related-only	relative_to config_dir as_posix path string config	src/mindroom/constants.py:855, src/mindroom/constants.py:884
_default_mind_knowledge_base_path	function	lines 139-148	none-found	MINDROOM_STORAGE_PATH agents/mind/workspace/memory mind_memory path	none
_ensure_mind_workspace	function	lines 151-153	none-found	ensure_workspace_template template mind force	none
_write_env_file	function	lines 156-184	duplicate-found	env_path write_text .env create overwrite append defaults	src/mindroom/cli/connect.py:99, src/mindroom/cli/local_stack.py:150
_append_missing_env_defaults	function	lines 187-210	duplicate-found	dotenv_values append missing env defaults write_text KEY=value	src/mindroom/cli/connect.py:112, src/mindroom/cli/local_stack.py:159
_should_replace_env_file	function	lines 213-217	none-found	Overwrite existing .env force typer.confirm	none
_config_init_env_hint	function	lines 220-232	none-found	config init env hint public codex vertexai_claude	none
_print_config_init_next_steps	function	lines 235-254	related-only	Next steps config edit validate run connect pair-code	src/mindroom/cli/main.py:341
_config_discovery_env	function	lines 257-262	related-only	exported_process_env MINDROOM_CONFIG_PATH path expanduser resolve	src/mindroom/constants.py:578, src/mindroom/constants.py:714
_resolve_config_path	function	lines 265-274	related-only	resolve_primary_runtime_paths config_path exported_process_env	src/mindroom/constants.py:359, src/mindroom/cli/main.py:282
_activate_cli_runtime	function	lines 277-293	related-only	resolve_primary_runtime_paths storage_path exported_process_env	src/mindroom/cli/main.py:282, src/mindroom/api/main.py:405
_get_editor	function	lines 296-313	none-found	EDITOR VISUAL nano vim vi notepad shutil.which	none
_format_validation_errors	function	lines 316-331	duplicate-found	iter_config_validation_messages Invalid configuration Issues found Fix these issues	src/mindroom/config/main.py:135, src/mindroom/api/config_lifecycle.py:120, src/mindroom/cli/doctor.py:120
config_init	function	lines 335-430	related-only	config init starter config env template owner placeholders workspace	src/mindroom/cli/connect.py:129, src/mindroom/config_template.yaml:1
config_show	function	lines 434-468	related-only	read config file syntax highlighting search locations missing config	src/mindroom/api/config_lifecycle.py:767, src/mindroom/cli/main.py:385
config_edit	function	lines 472-507	none-found	open config editor shlex subprocess run editor	none
config_validate	function	lines 511-547	related-only	load_config validate agents teams models rooms missing env keys	src/mindroom/cli/doctor.py:111
config_path_cmd	function	lines 551-564	duplicate-found	config_search_locations first match wins status exists not found	src/mindroom/cli/main.py:385, src/mindroom/cli/config.py:446
_load_config_quiet	function	lines 572-600	related-only	structlog configure reset_defaults load_config tolerate_plugin_load_errors	src/mindroom/cli/doctor.py:111, src/mindroom/config/main.py:1774
_find_missing_env_keys	function	lines 603-621	related-only	env_key_for_provider get_secret_from_env VERTEXAI_CLAUDE_ENV_KEYS providers_used	src/mindroom/cli/doctor.py:426, src/mindroom/cli/doctor.py:513, src/mindroom/cli/doctor.py:555
_resolve_config_init_selection	function	lines 624-653	none-found	normalize init profile provider preset minimal public-codex	none
_normalize_init_profile	function	lines 656-669	none-found	profile aliases public-vertexai-anthropic codex minimal full	none
_check_env_keys	function	lines 672-679	related-only	missing environment variables provider env_key warning	src/mindroom/cli/doctor.py:513, src/mindroom/cli/doctor.py:555
_normalize_provider_preset	function	lines 682-701	none-found	provider preset aliases anthropic openai openrouter vertexai_claude	none
_prompt_provider_preset	function	lines 704-715	none-found	typer.prompt provider preset invalid choice	none
_model_template_block	function	lines 718-731	related-only	DEFAULT_MODEL_PRESETS provider id context_window reasoning_effort	src/mindroom/config_template.yaml:5
_full_template	function	lines 734-873	related-only	starter config yaml assistant router authorization matrix_delivery knowledge_bases	src/mindroom/config_template.yaml:1
_env_template	function	lines 876-930	related-only	MATRIX_HOMESERVER MINDROOM_API_KEY provisioning storage_root provider env template	src/mindroom/cli/local_stack.py:150, src/mindroom/cli/connect.py:99
_minimal_template	function	lines 933-984	related-only	minimal config yaml assistant router mindroom_user authorization defaults	src/mindroom/config_template.yaml:1
_provider_env_template	function	lines 987-1018	related-only	provider API keys placeholders codex vertexai ANTHROPIC OPENAI OPENROUTER	src/mindroom/constants.py:1005, src/mindroom/cli/doctor.py:426
```

## Findings

1. `.env` mutation is implemented three ways.
`_write_env_file` and `_append_missing_env_defaults` create, overwrite, or append generated env values in `src/mindroom/cli/config.py:156` and `src/mindroom/cli/config.py:187`.
`persist_local_provisioning_env` in `src/mindroom/cli/connect.py:99` and `_persist_local_matrix_env` in `src/mindroom/cli/local_stack.py:150` independently resolve the env path, read existing lines, update selected `KEY=value` entries, and write the file back.
The behavior is functionally duplicated around preserving user-owned `.env` content while inserting MindRoom-managed keys.
Differences to preserve: `config.py` can generate a full replacement template and append a titled block of missing defaults, while `connect.py` and `local_stack.py` upsert specific values and preserve unrelated lines.

2. Config search-location display is repeated.
`config_show` and `config_path_cmd` both enumerate `config_search_locations(process_env)` and print indexed paths with rich status labels in `src/mindroom/cli/config.py:446` and `src/mindroom/cli/config.py:559`.
`_print_missing_config_error` repeats the same rendering in `src/mindroom/cli/main.py:385`.
The shared behavior is presenting the ordered config discovery list and existence status.
Differences to preserve: `config_show` uses this as missing-file help, `config_path_cmd` always prints it after the resolved path, and `main.py` adds surrounding quick-start/error text.

3. Invalid config formatting has overlapping CLI/API/message variants.
`_format_validation_errors` in `src/mindroom/cli/config.py:316` converts `iter_config_validation_messages()` into a rich CLI error with fix hints.
`format_invalid_config_message` in `src/mindroom/config/main.py:135`, `_validation_exception_errors` in `src/mindroom/api/config_lifecycle.py:120`, and `_check_config_valid` in `src/mindroom/cli/doctor.py:111` all consume the same iterator and format user-facing validation errors.
This is related duplication rather than a straight helper extraction because each surface has a different output medium.
Differences to preserve: CLI config wants multi-line rich output with next commands, doctor wants compact pass/fail output, API wants structured error objects, and Matrix/user messaging wants plain text.

4. Starter config YAML overlaps with `config_template.yaml`.
`_full_template` and `_minimal_template` render starter YAML in `src/mindroom/cli/config.py:734` and `src/mindroom/cli/config.py:933`.
`src/mindroom/config_template.yaml:1` contains a static starter config with many of the same sections: models, assistant agent, router, `mindroom_user`, `matrix_space`, `matrix_delivery`, authorization, and defaults.
This is related duplication because the CLI templates are now provider-aware and public-profile-aware, while the YAML file appears to be a broader reference template.

## Proposed Generalization

1. Add a small env-file helper in a focused CLI module, for example `src/mindroom/cli/env_file.py`, with `env_path_for_config(config_path)`, `upsert_env_values(env_path, values)`, and `append_missing_env_values(env_path, defaults, title)`.
2. Move only line-preserving `.env` update behavior into that helper and use it from config init, connect, and local-stack setup.
3. Add a small display helper for config search locations, for example `_print_config_search_locations(process_env, missing_status="not found")`, used by `config_show`, `config_path_cmd`, and `cli/main.py`.
4. Do not merge validation formatting yet; the existing shared iterator is the right central behavior, and the output surfaces differ enough that further consolidation would mostly hide formatting details.
5. Do not merge starter templates with `config_template.yaml` until the static template's role is clarified; generated templates have active profile/provider behavior that the static file does not.

## Risk/tests

The env helper would touch user `.env` preservation semantics, so tests should cover creating a new file, overwriting on force, preserving existing values, appending missing public defaults with correct blank-line separation, and upserting provisioning/local Matrix values without disturbing comments.
Search-location rendering can be covered with CLI runner snapshot-style assertions for `config show` missing-file output, `config path`, and top-level missing-config error.
Validation formatting should remain covered at the existing surface level if no refactor is made.
