# Duplication Audit: `src/mindroom/tool_system/metadata.py`

## Summary

Top duplication candidate: plugin module execution in `metadata.py` closely repeats the runtime plugin loader in `tool_system/plugins.py`, including package-chain installation/restoration, `spec_from_file_location`, `module_from_spec`, `sys.modules` insertion, and `exec_module`.
This is real behavior duplication, but the validation path has extra temporary-module cleanup semantics that must be preserved.

Related-only overlap: authored tool override normalization is centralized here, and config modules mostly call these helpers instead of reimplementing them.
The one nearby overlap is `AgentConfig.get_tool_overrides`, which filters per-agent override keys before delegating to `normalize_authored_tool_overrides`.

Related-only overlap: comma/newline string-list parsing exists for different domains (`metadata.py` authored string arrays, shell PATH prepend, shell extra env passthrough), but separators and return types differ enough that a shared helper is not clearly worth it.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ToolInitOverrideError	class	lines 61-62	none-found	ToolInitOverrideError ValueError unsupported tool init overrides	src/mindroom/tool_system/metadata.py:61; src/mindroom/tool_system/registry_state.py:10
ToolConfigOverrideError	class	lines 65-66	none-found	ToolConfigOverrideError ValueError authored tool config overrides	src/mindroom/tool_system/metadata.py:65; src/mindroom/config/main.py:1328
ToolAuthoredOverrideValidator	class	lines 69-73	none-found	ToolAuthoredOverrideValidator authored_override_validator MCP default	src/mindroom/tool_system/metadata.py:69; src/mindroom/mcp/registry.py:132
is_authored_override_inherit	function	lines 76-78	none-found	AUTHORED_OVERRIDE_INHERIT inherit sentinel authored override	src/mindroom/tool_system/metadata.py:76; src/mindroom/config/main.py:1356
apply_authored_overrides	function	lines 81-95	related-only	apply authored overrides inherit pop merge tool overrides	src/mindroom/tool_system/metadata.py:81; src/mindroom/config/main.py:1356; src/mindroom/config/main.py:1370
_sanitize_safe_tool_init_override_value	function	lines 98-121	related-only	base_dir shell_path_prepend safe init override pathlike string	src/mindroom/tool_system/metadata.py:98; src/mindroom/tools/shell.py:97; src/mindroom/constants.py:659
_override_path	function	lines 124-132	none-found	config_path_prefix tool field error path	src/mindroom/tool_system/metadata.py:124; src/mindroom/config/main.py:1328
_agent_override_field	function	lines 135-146	related-only	agent_override_fields field lookup metadata field.name	src/mindroom/tool_system/metadata.py:135; src/mindroom/config/agent.py:289
_validate_text_authored_override_value	function	lines 149-172	related-only	validate text authored override string array ConfigField	src/mindroom/tool_system/metadata.py:149; src/mindroom/config/agent.py:285; src/mindroom/mcp/registry.py:95
_validate_authored_override_value	function	lines 175-214	related-only	validate authored override boolean number text required null	src/mindroom/tool_system/metadata.py:175; src/mindroom/tool_system/metadata.py:1194
validate_authored_overrides	function	lines 217-260	related-only	validate authored overrides fields_by_name unknown password authored_override	src/mindroom/tool_system/metadata.py:217; src/mindroom/config/main.py:1328; src/mindroom/config/agent.py:285
_run_authored_override_validator	function	lines 263-278	related-only	authored override validator MCP validate_mcp_agent_overrides	src/mindroom/tool_system/metadata.py:263; src/mindroom/mcp/registry.py:95
validate_authored_tool_entry_overrides	function	lines 281-307	none-found	validate authored tool entry overrides tool-specific validation	src/mindroom/tool_system/metadata.py:281; src/mindroom/config/main.py:1328
sanitize_tool_init_overrides	function	lines 310-338	none-found	sanitize tool init overrides safe runtime init overrides	src/mindroom/tool_system/metadata.py:310; src/mindroom/api/sandbox_runner.py:230
_coerce_number_tool_config_value	function	lines 341-367	related-only	coerce number config value finite float int omit empty string	src/mindroom/tool_system/metadata.py:341; src/mindroom/tool_system/metadata.py:1194
_coerce_runtime_tool_config_value	function	lines 370-373	related-only	coerce runtime tool config value field type number	src/mindroom/tool_system/metadata.py:370; src/mindroom/tool_system/metadata.py:1194
_set_tool_config_init_kwarg	function	lines 376-385	none-found	set tool config init kwarg omit sentinel	src/mindroom/tool_system/metadata.py:376
_apply_tool_config_init_values	function	lines 388-409	related-only	apply tool config init values fields values skip inherited	src/mindroom/tool_system/metadata.py:388; src/mindroom/config/main.py:1356
_build_tool_config_init_kwargs	function	lines 412-439	none-found	build tool config init kwargs credentials overrides runtime base_dir	src/mindroom/tool_system/metadata.py:412; src/mindroom/agents.py:376
_build_managed_tool_init_kwargs	function	lines 442-464	none-found	managed init args runtime_paths credentials_manager worker_target	src/mindroom/tool_system/metadata.py:442; src/mindroom/tools/gmail.py:97; src/mindroom/tools/google_drive.py:65
_resolve_tool_credentials_manager	function	lines 467-478	none-found	resolve credentials manager config_fields managed init args	src/mindroom/tool_system/metadata.py:467; src/mindroom/oauth/client.py:127
_build_tool_instance	function	lines 481-574	related-only	build tool instance credentials sandbox proxy output files	src/mindroom/tool_system/metadata.py:481; src/mindroom/api/sandbox_runner.py:555; src/mindroom/agents.py:376
get_tool_by_name	function	lines 577-645	related-only	get tool by name registry dependencies auto install import retry	src/mindroom/tool_system/metadata.py:577; src/mindroom/agents.py:376; src/mindroom/api/sandbox_runner.py:555
ToolCategory	class	lines 648-661	not-a-behavior-symbol	enum tool categories values	src/mindroom/tool_system/metadata.py:648; src/mindroom/custom_tools/config_manager.py:914
ToolStatus	class	lines 664-668	not-a-behavior-symbol	enum tool status values	src/mindroom/tool_system/metadata.py:664; src/mindroom/custom_tools/config_manager.py:932
SetupType	class	lines 671-677	not-a-behavior-symbol	enum setup type values	src/mindroom/tool_system/metadata.py:671; src/mindroom/tools/*.py:13
ToolExecutionTarget	class	lines 680-684	not-a-behavior-symbol	enum execution target primary worker	src/mindroom/tool_system/metadata.py:680; src/mindroom/tools/file.py:274; src/mindroom/tools/python.py:69
ToolManagedInitArg	class	lines 687-694	not-a-behavior-symbol	enum managed init arg values	src/mindroom/tool_system/metadata.py:687; src/mindroom/tools/gmail.py:97; src/mindroom/tools/google_drive.py:65
ConfigField	class	lines 698-710	not-a-behavior-symbol	dataclass config field schema ConfigField	src/mindroom/tool_system/metadata.py:698; src/mindroom/mcp/registry.py:68; src/mindroom/tools/*.py:22
ToolValidationInfo	class	lines 714-721	not-a-behavior-symbol	dataclass validation only tool metadata snapshot	src/mindroom/tool_system/metadata.py:714; src/mindroom/config/main.py:1274
ToolMetadata	class	lines 725-746	not-a-behavior-symbol	dataclass complete metadata tool registry	src/mindroom/tool_system/metadata.py:725; src/mindroom/tool_system/registry_state.py:155
register_tool_with_metadata	function	lines 749-841	related-only	decorator register tool metadata builtin plugin registration	src/mindroom/tool_system/metadata.py:749; src/mindroom/tool_system/registry_state.py:155; src/mindroom/tools/*.py:13
register_tool_with_metadata.<locals>.decorator	nested_function	lines 800-839	related-only	decorator metadata object plugin registration owner module	src/mindroom/tool_system/metadata.py:800; src/mindroom/tool_system/registry_state.py:155
_resolved_module_file	function	lines 845-850	related-only	resolve module file cached is_relative_to plugin root	src/mindroom/tool_system/metadata.py:845; src/mindroom/tool_system/plugin_imports.py:228; src/mindroom/workspaces.py:55
_module_origin_within_root	function	lines 853-859	related-only	module __file__ origin within root is_relative_to	src/mindroom/tool_system/metadata.py:853; src/mindroom/tool_system/plugin_imports.py:228
_execute_validation_plugin_module	function	lines 862-912	duplicate-found	spec_from_file_location module_from_spec exec_module plugin package chain sys.modules	src/mindroom/tool_system/metadata.py:862; src/mindroom/tool_system/plugins.py:430
resolved_tool_state_for_runtime	function	lines 915-999	related-only	resolve tool state runtime plugins MCP builtins registry metadata	src/mindroom/tool_system/metadata.py:915; src/mindroom/tool_system/registry_state.py:120; src/mindroom/tool_system/plugins.py:400
_merge_mcp_tool_state	function	lines 1002-1014	related-only	merge MCP tool state collision registry metadata	src/mindroom/tool_system/metadata.py:1002; src/mindroom/tool_system/registry_state.py:120
resolved_tool_metadata_for_runtime	function	lines 1017-1029	none-found	resolved tool metadata for runtime wrapper state	src/mindroom/tool_system/metadata.py:1017; src/mindroom/api/tools.py:28
tool_validation_snapshot_from_state	function	lines 1032-1046	related-only	project tool metadata validation snapshot config_fields runtime_loadable	src/mindroom/tool_system/metadata.py:1032; src/mindroom/tool_system/metadata.py:1064
resolved_tool_validation_snapshot_for_runtime	function	lines 1049-1061	none-found	resolved tool validation snapshot runtime wrapper	src/mindroom/tool_system/metadata.py:1049; src/mindroom/config/main.py:1274
serialize_tool_validation_snapshot	function	lines 1064-1076	related-only	serialize validation snapshot asdict config_fields runtime_loadable	src/mindroom/tool_system/metadata.py:1064; src/mindroom/constants.py:512; src/mindroom/interactive.py:137
_deserialize_tool_validation_fields	function	lines 1079-1092	related-only	deserialize config field list object ConfigField TypeError	src/mindroom/tool_system/metadata.py:1079; src/mindroom/thread_tags.py:160; src/mindroom/custom_tools/matrix_api.py:304
deserialize_tool_validation_snapshot	function	lines 1095-1140	related-only	deserialize validation snapshot JSON object keyed by tool name	src/mindroom/tool_system/metadata.py:1095; src/mindroom/constants.py:512; src/mindroom/interactive.py:143
default_worker_routed_tools	function	lines 1143-1150	none-found	default worker routed tools default_execution_target worker	src/mindroom/tool_system/metadata.py:1143; src/mindroom/config/main.py:1159
export_tools_metadata	function	lines 1153-1170	related-only	export tools metadata asdict enum values sorted category name	src/mindroom/tool_system/metadata.py:1153; src/mindroom/api/tools.py:323; src/mindroom/custom_tools/config_manager.py:535
_normalize_string_array_override	function	lines 1173-1191	related-only	normalize string array override comma newline separated list strings	src/mindroom/tool_system/metadata.py:1173; src/mindroom/tools/shell.py:97; src/mindroom/constants.py:659
_normalize_agent_override_field_value	function	lines 1194-1216	related-only	normalize agent override field value boolean number string array text	src/mindroom/tool_system/metadata.py:1194; src/mindroom/tool_system/metadata.py:175
normalize_authored_tool_overrides	function	lines 1219-1256	related-only	normalize authored tool overrides agent_override_fields unsupported per-agent overrides	src/mindroom/tool_system/metadata.py:1219; src/mindroom/config/agent.py:285
authored_tool_overrides_to_runtime	function	lines 1259-1274	related-only	authored tool overrides to runtime string array join	src/mindroom/tool_system/metadata.py:1259; src/mindroom/config/main.py:1397
```

## Findings

### 1. Validation plugin execution duplicates runtime plugin loading

`src/mindroom/tool_system/metadata.py:862` and `src/mindroom/tool_system/plugins.py:430` both implement the same core plugin module execution sequence.
Both compute a plugin module name, snapshot/install package-chain entries, build an importlib spec from a file path, create a module from the spec, insert it into `sys.modules`, execute it, and restore package-chain state on failure or completion.

The duplicated behavior is active and non-trivial because both paths must preserve correct plugin package imports and registration ownership.
The differences are important: `metadata.py` creates a unique validation module name and removes modules loaded from the plugin root after validation, while `plugins.py` maintains a runtime cache, handles mtime checks, and restores failed tool-module reloads.

### 2. Per-agent override key filtering is adjacent to centralized override normalization

`src/mindroom/config/agent.py:285` filters `entry.overrides` against `metadata.agent_override_fields` before calling `normalize_authored_tool_overrides`.
`src/mindroom/tool_system/metadata.py:1219` repeats the same field-map construction and unsupported-field validation for full normalization.

This is related but not a strong refactor target.
The config method intentionally ignores non-agent override keys because `entry.overrides` can contain constructor/config overrides as well as per-agent runtime overrides, while `normalize_authored_tool_overrides` intentionally raises for unsupported keys when called with a pure per-agent override mapping.

### 3. String-list parsing is similar but domain-specific

`src/mindroom/tool_system/metadata.py:1173`, `src/mindroom/tools/shell.py:97`, and `src/mindroom/constants.py:659` all parse delimited strings into stripped non-empty entries.
The behavior is not identical: authored override string arrays accept Python lists and comma/newline text, shell PATH prepend accepts comma/newline text, and shell extra env passthrough accepts whitespace/comma text.

This is related-only duplication.
A shared parser would need separator and input-type knobs, which is likely more abstraction than the current small helpers justify.

## Proposed Generalization

For finding 1, consider a small internal helper in `mindroom.tool_system.plugin_imports`, for example `execute_plugin_module_from_path(...)`, that owns package-chain install/restore, spec creation, module creation, `sys.modules` insertion, and scoped registration owner handling.
Keep runtime caching/reload policy in `plugins.py` and validation-only root cleanup in `metadata.py`.

Minimal plan:

1. Add a helper that accepts `plugin_name`, `plugin_root`, `module_path`, `module_name`, and an optional execution context manager.
2. Move only the common package-chain/importlib execution skeleton into that helper.
3. Leave validation module naming and cleanup of modules within `plugin_root` in `metadata.py`.
4. Leave runtime cache, mtime, and failed reload restoration in `plugins.py`.
5. Add focused tests covering one successful validation load, one failed validation load, and one runtime plugin load.

No refactor recommended for findings 2 or 3.

## Risk/tests

The plugin loader duplication is risky to refactor because import caching, package-chain restoration, and `sys.modules` cleanup affect plugin isolation.
Tests should cover successful import, import failure restoration, repeated runtime loads with cache hits, validation execution without global registry mutation, and cleanup of transient modules under the plugin root.

The override normalization related-only areas should not be changed without tests for mixed config/per-agent override mappings, inherited override sentinels, MCP include/exclude validation, and string-array conversion to runtime comma-separated strings.
