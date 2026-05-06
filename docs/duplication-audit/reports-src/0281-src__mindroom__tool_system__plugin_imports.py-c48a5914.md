Summary: The top duplication candidate is dynamic Python module execution.
`src/mindroom/tool_system/plugins.py` and `src/mindroom/tool_system/metadata.py` both perform plugin module import setup, execution, and cleanup around the package-chain helpers in `plugin_imports.py`.
Most other symbols in `plugin_imports.py` are the canonical implementation already reused by plugin loading, metadata validation, OAuth loading, sandbox config filtering, and plugin root watching, so no broader refactor is recommended.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
PluginValidationError	class	lines 25-26	related-only	PluginValidationError validation errors plugin imports	src/mindroom/config/main.py:66; src/mindroom/oauth/registry.py:58; src/mindroom/tool_system/plugins.py:38
_PluginManifest	class	lines 30-37	none-found	_PluginManifest manifest dataclass tools_module hooks_module oauth_module	none
_PluginBase	class	lines 41-50	related-only	_PluginBase plugin loaded details manifest_path skill_dirs	src/mindroom/tool_system/plugins.py:53; src/mindroom/hooks/registry.py:47; src/mindroom/oauth/registry.py:90
_PluginCacheEntry	class	lines 54-56	related-only	PluginCacheEntry manifest_mtime cache entry	src/mindroom/oauth/registry.py:29; src/mindroom/tool_system/plugins.py:42
_ModuleCacheEntry	class	lines 60-63	related-only	ModuleCacheEntry module import cache mtime module_name	src/mindroom/tool_approval.py:128; src/mindroom/tool_system/plugins.py:417; src/mindroom/tool_system/registry_state.py:42
_warn_once	function	lines 70-76	none-found	warn_once _WARNED warning_key once per path	none
_collect_plugin_bases	function	lines 79-102	related-only	collect plugin bases skip_broken_plugins plugin_entries	src/mindroom/tool_system/plugins.py:133; src/mindroom/tool_system/metadata.py:938; src/mindroom/oauth/registry.py:84
_log_skipped_plugin_entry	function	lines 105-124	related-only	skipped plugin entry skip_broken_plugins log warning	src/mindroom/tool_system/plugins.py:150; src/mindroom/tool_system/metadata.py:985; src/mindroom/oauth/registry.py:108; src/mindroom/api/sandbox_runner.py:190
_reject_duplicate_plugin_manifest_names	function	lines 127-144	related-only	duplicate plugin manifest names paths by name	src/mindroom/tool_system/plugins.py:140; src/mindroom/tool_system/metadata.py:944; src/mindroom/oauth/registry.py:89
_resolve_plugin_root	function	lines 147-164	related-only	resolve plugin root python module config relative path	src/mindroom/tool_system/plugins.py:184; src/mindroom/api/sandbox_runner.py:191; src/mindroom/api/sandbox_runner.py:196
_resolve_python_plugin_root	function	lines 167-191	none-found	resolve python plugin root find_spec submodule_search_locations origin	none
_parse_python_plugin_spec	function	lines 194-214	none-found	python pkg module prefix plugin spec split colon	none
_load_plugin_base	function	lines 217-258	none-found	load plugin base manifest mtime cache resolve modules skills	none
_parse_manifest	function	lines 261-318	none-found	parse plugin manifest json tools_module hooks_module oauth_module skills	none
_resolve_module_path	function	lines 321-329	none-found	resolve module path plugin tools hooks oauth file exists	none
_resolve_skill_dirs	function	lines 332-341	none-found	resolve skill dirs plugin skills directory exists	none
_plugin_slug	function	lines 344-345	related-only	slug sanitize plugin name regex module name	src/mindroom/tool_system/plugin_identity.py:1; src/mindroom/tool_system/runtime_context.py:547
_plugin_package_name	function	lines 348-350	none-found	mindroom_plugin package hash plugin root	none
_relative_module_name	function	lines 353-355	none-found	relative module name plugin root suffix parts slug	none
_module_name	function	lines 358-359	related-only	plugin module name synthetic module name	src/mindroom/tool_system/plugins.py:94; src/mindroom/tool_system/plugins.py:416; src/mindroom/tool_system/metadata.py:869
_package_chain_names	function	lines 362-372	related-only	package chain names synthetic package root relative parent	src/mindroom/tool_system/plugins.py:428; src/mindroom/tool_system/metadata.py:871
_snapshot_plugin_package_chain	function	lines 375-383	duplicate-found	snapshot package chain sys.modules plugin package import context	src/mindroom/tool_system/plugins.py:428; src/mindroom/tool_system/metadata.py:871
_install_plugin_package_chain	function	lines 386-398	duplicate-found	install package chain ModuleType __path__ spec_from_file_location	src/mindroom/tool_system/plugins.py:429; src/mindroom/tool_system/metadata.py:872
_restore_plugin_package_chain	function	lines 401-406	duplicate-found	restore package chain sys.modules cleanup validation runtime import	src/mindroom/tool_system/plugins.py:432; src/mindroom/tool_system/plugins.py:460; src/mindroom/tool_system/metadata.py:875; src/mindroom/tool_system/metadata.py:906
```

Findings:

1. Dynamic plugin module execution scaffolding is repeated in runtime loading and validation loading.
`src/mindroom/tool_system/plugins.py:428` through `src/mindroom/tool_system/plugins.py:444` snapshots and installs the synthetic package chain, builds a file-location spec, creates a module, stores it in `sys.modules`, and executes it.
`src/mindroom/tool_system/metadata.py:871` through `src/mindroom/tool_system/metadata.py:893` repeats the same package-chain setup, spec construction, module creation, `sys.modules` insertion, and execution for validation imports.
Both paths depend on `_module_name`, `_snapshot_plugin_package_chain`, `_install_plugin_package_chain`, and `_restore_plugin_package_chain`, and both must restore package state if spec creation fails or execution exits.
The differences to preserve are meaningful: runtime loading uses the stable module name and `_scoped_plugin_registration_owner` only for tools, while validation loading uses a unique suffixed module name, a temporary registration store, and additional cleanup of modules originating under the plugin root.

2. Dynamic file import with mtime caching appears in plugin loading and approval script loading, but it is only related.
`src/mindroom/tool_system/plugins.py:409` through `src/mindroom/tool_system/plugins.py:469` and `src/mindroom/tool_approval.py:128` through `src/mindroom/tool_approval.py:150` both stat a Python file, use `spec_from_file_location`, create a module, execute it, and cache by path/mtime.
The behavior is not a direct duplication because plugin imports require synthetic package support, tool registration rollback, and plugin cache eviction, while approval scripts use UUID module names and a lock-protected script cache.
No shared helper is recommended unless more non-plugin file importers appear.

Proposed generalization:

Extract a small context helper in `src/mindroom/tool_system/plugin_imports.py`, for example `_plugin_package_context(plugin_name, plugin_root, module_path)`, that snapshots, installs, and always restores the package chain.
Update `plugins._load_plugin_module` and `metadata._execute_validation_plugin_module` to wrap their existing spec and execution logic in that context while keeping their current module naming, registration scope, cache handling, and validation-only root cleanup local.
Do not merge runtime plugin loading and validation loading into one loader because their rollback and registry semantics differ.

Risk/tests:

The risk is import-state leakage or accidental removal of modules that validation currently restores explicitly.
Focused tests should cover plugin reload success, plugin reload failure rollback, validation-only import cleanup, relative imports from plugin subpackages, duplicate manifest rejection, and OAuth module loading.
Relevant existing tests appear in `tests/test_plugins.py`, `tests/test_config_reload.py`, `tests/test_tool_hooks.py`, `tests/api/test_oauth_api.py`, and `tests/api/test_sandbox_runner_api.py`.
