Summary: The strongest duplication is the dynamic plugin module import transaction shared by runtime plugin loading and metadata validation loading.
A smaller duplicate pattern exists around evicting synthetic plugin modules from `sys.modules`.
Most other symbols in `src/mindroom/tool_system/plugins.py` are plugin-runtime-specific or only related to callers/tests, not duplicated behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_ConfiguredPluginRootCacheKey	class	lines 42-47	none-found	configured plugin root cache key runtime_paths sys_path	none
_Plugin	class	lines 54-65	related-only	Plugin dataclass plugin base loaded plugin details	src/mindroom/tool_system/plugin_imports.py:40
PluginReloadResult	class	lines 69-74	related-only	PluginReloadResult reload_plugins result active_plugin_names cancelled_task_count	src/mindroom/commands/handler.py:180; src/mindroom/orchestrator.py:793; src/mindroom/runtime_protocols.py:58
PreparedPluginReload	class	lines 78-84	none-found	PreparedPluginReload prepared plugin runtime snapshot tool_registry_snapshot plugin_skill_roots	none
_hook_display_name	function	lines 87-88	none-found	hook display name callback __name__ discovered plugin hooks	none
_sync_loaded_plugin_tools	function	lines 91-98	related-only	synchronize plugin tools active module names plugin overlay	src/mindroom/tool_system/registry_state.py:141
deactivate_plugins	function	lines 101-111	none-found	deactivate plugins clear plugin tools skills hooks oauth cache	none
load_plugins	function	lines 114-166	related-only	collect plugin bases materialize plugin set skill roots snapshot registry	src/mindroom/oauth/registry.py:77; src/mindroom/tool_system/plugin_imports.py:79; src/mindroom/tool_system/bootstrap.py:22
get_configured_plugin_roots	function	lines 169-191	related-only	resolve enabled plugin roots skip invalid cache dict.fromkeys	src/mindroom/api/sandbox_runner.py:176; src/mindroom/orchestration/plugin_watch.py:98
_configured_plugin_root_cache_key	function	lines 194-204	none-found	configured plugin root cache key plugin entries config path sys.path	none
_clear_configured_plugin_roots_cache	function	lines 207-209	related-only	clear configured plugin roots cache cache invalidation	src/mindroom/workers/runtime.py:56; src/mindroom/tool_system/skills.py:326; src/mindroom/tool_approval.py:158
_clear_oauth_provider_cache_after_plugin_change	function	lines 212-216	related-only	clear oauth provider cache plugin change import cycle	src/mindroom/oauth/registry.py:38
prepare_plugin_reload	function	lines 219-242	related-only	prepare plugin reload snapshot restore tool registry skill roots	src/mindroom/orchestrator.py:842; src/mindroom/tool_system/registry_state.py:183
apply_prepared_plugin_reload	function	lines 245-264	related-only	apply prepared plugin reload restore snapshot skill roots cancel existing tasks	src/mindroom/orchestrator.py:855; src/mindroom/tool_system/registry_state.py:203
reload_plugins	function	lines 267-284	related-only	reload plugins cancel module tasks prepare apply	src/mindroom/orchestrator.py:793; src/mindroom/orchestration/plugin_watch.py:31; src/mindroom/commands/handler.py:91
_cancel_plugin_module_tasks	function	lines 287-304	related-only	cancel module global asyncio tasks dedupe task ids	src/mindroom/background_tasks.py:81; src/mindroom/scheduling.py:416; src/mindroom/tools/shell.py:582
_iter_module_tasks	function	lines 307-317	none-found	iter module tasks asyncio Task container dict list tuple set	none
_clear_plugin_reload_caches	function	lines 320-325	related-only	clear plugin reload caches manifest module oauth configured roots	src/mindroom/api/runtime_reload.py:100; src/mindroom/orchestrator.py:1143; src/mindroom/workers/runtime.py:56
_evict_synthetic_plugin_subtrees	function	lines 328-332	duplicate-found	evict synthetic plugin subtrees sys.modules startswith plugin root	src/mindroom/tool_system/registry_state.py:222; src/mindroom/tool_system/metadata.py:898
_materialize_plugin	function	lines 335-364	related-only	load plugin tools hooks discover hooks build plugin dataclass	src/mindroom/oauth/registry.py:77; src/mindroom/hooks/__init__.py:1
_prepare_plugin_tool_module_reload	function	lines 367-380	none-found	snapshot clear plugin tool registrations before reload candidate module names	none
_restore_failed_plugin_tool_module_reload	function	lines 383-397	related-only	restore failed plugin tool module reload sys.modules cache registrations	src/mindroom/tool_system/registry_state.py:203
_load_plugin_module	function	lines 400-470	duplicate-found	spec_from_file_location module_from_spec plugin package chain exec_module restore sys.modules cache	src/mindroom/tool_system/metadata.py:862; src/mindroom/tool_approval.py:135; src/mindroom/oauth/registry.py:94
```

## Findings

1. Dynamic plugin module import transaction is duplicated.

`src/mindroom/tool_system/plugins.py:400` loads runtime plugin modules by computing a synthetic module name, snapshotting/installing the plugin package chain, creating an importlib spec, inserting the module in `sys.modules`, executing it, and restoring package/module state on failure.
`src/mindroom/tool_system/metadata.py:862` repeats the same core import transaction for validation plugin modules: it computes the runtime module name, installs the same plugin package chain, creates a spec, inserts a module in `sys.modules`, executes under plugin registration scopes, and restores `sys.modules`/package-chain state in `finally`.

The behavior is functionally the same at the import-transaction level.
Differences to preserve: runtime loading uses mtime caching and `_MODULE_IMPORT_CACHE`; tool modules snapshot and clear plugin tool registrations before execution; validation loading uses a unique validation module suffix, a temporary registration store, and removes modules originating inside the plugin root after execution.
`src/mindroom/tool_approval.py:135` is related dynamic script loading, but it lacks synthetic plugin package-chain handling and plugin registry rollback, so it is not a direct duplicate.
`src/mindroom/oauth/registry.py:94` calls `_load_plugin_module` for OAuth plugin modules rather than duplicating the loader.

2. Synthetic plugin module eviction is repeated in a narrower form.

`src/mindroom/tool_system/plugins.py:328` removes imported synthetic plugin modules when their module name is one of the package roots or starts with one of those roots plus `.`.
`src/mindroom/tool_system/registry_state.py:222` removes synthetic plugin modules during snapshot restore when a module starts with `_PLUGIN_MODULE_PREFIX` and is absent from the snapshot.
Both are `sys.modules` cleanup passes over synthetic plugin modules.

The behavior overlaps but is not identical.
The plugin reload path targets a supplied set of package roots, while snapshot restore targets every module with the global synthetic-plugin prefix that was not present in the snapshot.
`src/mindroom/tool_system/metadata.py:898` is related cleanup after validation imports, but it filters by module origin under the plugin root rather than by synthetic module name.

## Proposed Generalization

A minimal helper could live in `src/mindroom/tool_system/plugin_imports.py` because that module already owns plugin synthetic names and package-chain helpers.

1. Add a small internal context/helper for "execute a plugin file module under an installed package chain" that accepts the final module name, plugin identity/root/path, and an execution callback or context managers.
2. Reuse it from `_load_plugin_module` and `_execute_validation_plugin_module`, keeping runtime cache/registration rollback outside the helper.
3. Add a narrow `evict_plugin_modules(package_roots: set[str] | None = None, *, snapshot_names: set[str] | None = None)` only if the call sites remain simpler than their current loops.

No broad plugin architecture refactor is recommended.
The loader has enough special cases that the first helper should only own the importlib/package-chain/sys.modules transaction, not plugin registry semantics.

## Risk/tests

The import-transaction refactor would be risky around rollback semantics.
Tests to run or extend: `tests/test_plugins.py` reload/helper-module invalidation cases, OAuth plugin loading tests, and `tests/test_tools_metadata.py` validation plugin execution tests.
Specific behavior to protect: failed runtime tool reload restores previous registrations and module cache; failed hooks/OAuth import restores cached module state; validation import leaves no extra modules from the plugin root; package-chain imports still support relative imports inside plugin modules.

Coverage complete: all 23 required symbols are represented in the TSV.
