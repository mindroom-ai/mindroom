## Summary

Top duplication candidates in `src/mindroom/commands/config_commands.py` are the runtime config edit pipeline shared with `src/mindroom/custom_tools/config_manager.py` and `src/mindroom/custom_tools/self_config.py`, plus repeated YAML formatting for displaying authored config fragments.
The dot-path get/set behavior is local to the `!config` command surface; no equivalent generic nested-path helper was found under `./src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_parse_config_args	function	lines 28-54	related-only	shlex.split args_text command parser CONFIG_PATTERN	src/mindroom/commands/parsing.py:94; src/mindroom/commands/parsing.py:179; src/mindroom/cli/config.py:490
_get_nested_value	function	lines 57-81	none-found	path.split(".") get nested value digit index current[key]	none
_set_nested_value	function	lines 84-115	none-found	path.split(".") set nested value digit index auto-create dict current[key]	none
_parse_value	function	lines 118-142	related-only	yaml.safe_load value string parse JSON YAML	src/mindroom/config/main.py:966; src/mindroom/config/main.py:1762; src/mindroom/api/sandbox_runner.py:159; src/mindroom/tool_system/skills.py:391
_validate_config_dict	function	lines 145-147	duplicate-found	Config.validate_with_runtime authored_model_dump persist_runtime_validated_config validated payload	src/mindroom/custom_tools/config_manager.py:83; src/mindroom/api/config_lifecycle.py:250; src/mindroom/api/config_lifecycle.py:360
_format_value	function	lines 150-166	duplicate-found	yaml.dump default_flow_style False sort_keys False config display authored_model_dump	src/mindroom/custom_tools/self_config.py:61; src/mindroom/custom_tools/config_manager.py:899; src/mindroom/commands/config_commands.py:201
handle_config_command	async_function	lines 169-295	duplicate-found	load_config_or_user_error authored_model_dump validate preview YAML config mutation rejected message	src/mindroom/custom_tools/self_config.py:43; src/mindroom/custom_tools/self_config.py:114; src/mindroom/custom_tools/config_manager.py:307; src/mindroom/custom_tools/config_manager.py:668
apply_config_change	async_function	lines 298-345	duplicate-found	load_config_or_user_error set validate persist_runtime_validated_config format_invalid_config_message	src/mindroom/custom_tools/config_manager.py:83; src/mindroom/custom_tools/config_manager.py:646; src/mindroom/custom_tools/config_manager.py:761; src/mindroom/custom_tools/config_manager.py:810; src/mindroom/custom_tools/self_config.py:209
```

## Findings

1. Runtime config mutation has repeated load, validate, persist, and error-message behavior across chat commands and tools.

- `src/mindroom/commands/config_commands.py:189` loads the current config for `!config`, validates a candidate at `src/mindroom/commands/config_commands.py:248`, then persists after confirmation at `src/mindroom/commands/config_commands.py:338`.
- `src/mindroom/custom_tools/config_manager.py:83` defines `_save_runtime_validated_config`, which validates `config.authored_model_dump()` and calls `config_lifecycle.persist_runtime_validated_config`.
- `src/mindroom/custom_tools/config_manager.py:646`, `src/mindroom/custom_tools/config_manager.py:761`, and `src/mindroom/custom_tools/config_manager.py:810` apply agent/team edits and save through the same validation/persistence path.
- `src/mindroom/custom_tools/self_config.py:114` loads config with the same rejection footer before edits, mutates an agent, and saves at `src/mindroom/custom_tools/self_config.py:209`.
- The shared behavior is functionally the same: load the active runtime config while tolerating plugin load errors, mutate a candidate, validate against runtime paths, persist through `config_lifecycle`, and return a user-facing invalid-config message on `ValidationError` or `ConfigRuntimeValidationError`.
- Differences to preserve: `!config` supports arbitrary dot paths and a reaction confirmation preview; `config_manager` and `self_config` mutate typed agent/team fields directly and have different success text.

2. YAML display formatting for config fragments is duplicated.

- `src/mindroom/commands/config_commands.py:150` formats arbitrary values with `yaml.dump(default_flow_style=False, sort_keys=False, allow_unicode=True)` and trims the trailing document marker.
- `src/mindroom/commands/config_commands.py:201` separately dumps the whole authored config with the same YAML options but no marker trimming helper.
- `src/mindroom/custom_tools/self_config.py:61` and `src/mindroom/custom_tools/config_manager.py:899` both dump authored agent config fragments for markdown code blocks with `default_flow_style=False` and `sort_keys=False`.
- The shared behavior is presenting authored config data as YAML in chat/tool responses.
- Differences to preserve: command formatting currently enables `allow_unicode=True` and strips `...`; tool formatting does not explicitly set `allow_unicode` or trim markers.

3. Config command argument parsing is related to, but not duplicated with, the global command parser.

- `src/mindroom/commands/parsing.py:94` extracts the raw `!config` argument tail with a regex and strips it at `src/mindroom/commands/parsing.py:182`.
- `src/mindroom/commands/config_commands.py:28` then uses `shlex.split` to preserve quoted values and returns a domain-specific `parse_error` operation for unmatched quotes.
- This is a two-stage parse for different responsibilities rather than duplicate behavior.

4. Dot-path get/set helpers appear local to `!config`.

- Searches for `path.split(".")`, digit-index traversal, and nested `current[key]` mutation found no equivalent generic helper under `./src`.
- The closest config mutation paths in `config_manager` and `self_config` use typed model fields instead of arbitrary dot notation.

## Proposed Generalization

1. Add a small helper in a focused config command/tool support module, for example `mindroom.config.runtime_edit`, that wraps "load current config with plugin-load tolerance, apply mutation, validate with runtime, persist, and format validation errors".
2. Keep path traversal out of that helper; `!config` can still supply a mutator that calls `_set_nested_value`, while config tools can supply typed mutators.
3. Add a single YAML display helper for authored config fragments, likely near config presentation code, with options matching the stricter command behavior: `default_flow_style=False`, `sort_keys=False`, `allow_unicode=True`, and trailing `...` trimming.
4. Migrate one caller first, preferably `config_commands.py`, then consider `self_config.py` and `config_manager.py` only if tests show identical behavior can be preserved.
5. Do not generalize `_parse_config_args`, `_get_nested_value`, or `_set_nested_value` unless another arbitrary dot-path editing surface is introduced.

## Risk/tests

- Main behavior risk is changing config serialization details, especially key ordering, Unicode handling, and marker/newline trimming in chat/tool responses.
- Another risk is accidentally removing the `!config` confirmation boundary; that preview-and-reaction flow must remain separate from immediate tool writes.
- Tests should cover `!config show`, `!config get`, `!config set` preview, confirmed application, invalid config rejection, YAML list/dict value parsing, unmatched quote parse errors, and at least one config-manager or self-config update path after any shared helper extraction.
