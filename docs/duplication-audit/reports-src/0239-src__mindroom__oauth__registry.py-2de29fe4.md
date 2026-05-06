## Summary

Top duplication candidates for `src/mindroom/oauth/registry.py`:

- `load_oauth_providers` and `load_oauth_providers_for_snapshot` duplicate the same cache-read, plugin-provider load, builtin-provider merge, registry validation, cache-write, and logging flow.
- `_load_plugin_oauth_providers` repeats the plugin-base collection, duplicate-manifest rejection, per-plugin module loading, skip-broken error handling, and skipped-entry logging pattern used by `src/mindroom/tool_system/plugins.py`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_ProviderCacheEntry	class	lines 30-32	related-only	cache entry dataclass provider cache module cache	`src/mindroom/tool_system/plugin_imports.py:53`, `src/mindroom/tool_system/plugin_imports.py:59`, `src/mindroom/tool_system/plugins.py:42`
clear_oauth_provider_cache	function	lines 38-42	related-only	clear cache plugin reload cache oauth provider cache	`src/mindroom/tool_system/plugins.py:207`, `src/mindroom/tool_system/plugins.py:212`, `src/mindroom/tool_system/plugins.py:320`, `src/mindroom/workers/runtime.py:56`, `src/mindroom/tool_system/skills.py:326`
_builtin_oauth_providers	function	lines 45-51	none-found	builtin oauth providers google_calendar google_drive google_gmail google_sheets	none
_module_oauth_provider_callback	function	lines 54-59	related-only	register_oauth_providers callback callable iter_module_hooks module hooks	`src/mindroom/hooks/decorators.py:72`, `src/mindroom/tool_system/plugins.py:347`
_coerce_oauth_providers	function	lines 62-74	related-only	coerce oauth providers iterable OAuthProvider PluginValidationError return iterable	`src/mindroom/hooks/decorators.py:72`, `src/mindroom/tool_system/plugin_imports.py:304`
_load_plugin_oauth_providers	function	lines 77-109	duplicate-found	collect_plugin_bases reject_duplicate_plugin_manifest_names load_plugin_module skip_broken_plugins log_skipped_plugin_entry	`src/mindroom/tool_system/plugins.py:114`, `src/mindroom/tool_system/plugins.py:133`, `src/mindroom/tool_system/plugins.py:140`, `src/mindroom/tool_system/plugins.py:142`, `src/mindroom/tool_system/plugins.py:145`, `src/mindroom/tool_system/plugins.py:150`
_provider_registry	function	lines 112-149	none-found	duplicate OAuth provider id service_owners duplicate_services shared_client_config_service	none
_registered_tool_service_auth_providers	function	lines 152-155	related-only	TOOL_METADATA auth_provider tool service auth providers	`src/mindroom/api/tools.py:243`, `src/mindroom/tool_system/metadata.py:741`, `src/mindroom/tool_system/registry_state.py:22`
_reject_tool_service_collisions	function	lines 158-179	related-only	service collision tool metadata auth_provider existing registered tool conflicts	`src/mindroom/mcp/registry.py:166`, `src/mindroom/mcp/registry.py:192`
load_oauth_providers	function	lines 182-204	duplicate-found	load oauth providers cache_key provider cache builtin plugin provider_registry logger Loaded OAuth providers	`src/mindroom/oauth/registry.py:207`
load_oauth_providers_for_snapshot	function	lines 207-233	duplicate-found	load oauth providers snapshot cache_key provider cache builtin plugin provider_registry logger Loaded OAuth providers	`src/mindroom/oauth/registry.py:182`
```

## Findings

1. Duplicated OAuth provider load/cache body in the two public loaders.

- `src/mindroom/oauth/registry.py:182` builds a cache key for a live `Config`, checks `_provider_cache`, calls `_load_plugin_oauth_providers`, combines builtins and plugin providers, validates with `_provider_registry`, writes `_ProviderCacheEntry`, logs, and returns.
- `src/mindroom/oauth/registry.py:207` does the same after deriving a `Config` from `ApiSnapshot`.
- The duplicated behavior is the provider materialization and cache update sequence.
- The difference to preserve is only how the `Config` and cache key are derived: direct config uses `("config", id(config), runtime_paths, skip_broken_plugins)`, while snapshot uses `("snapshot", snapshot.generation, id(snapshot), snapshot.runtime_paths, skip_broken_plugins)` and may hydrate a config from `snapshot.config_data`.

2. OAuth plugin-provider loading repeats the general plugin-loader traversal.

- `src/mindroom/oauth/registry.py:77` calls `plugin_imports._collect_plugin_bases`, rejects duplicate manifest names, loops over plugin bases, skips entries without an OAuth module, loads a plugin module, invokes plugin-specific discovery, and logs or raises depending on `skip_broken_plugins`.
- `src/mindroom/tool_system/plugins.py:114` uses the same collect/reject/loop/materialize/skip-broken pattern for tool, hook, and skill plugin materialization.
- The shared behavior is resolving configured plugin entries and applying one per-plugin materialization function with consistent duplicate-name and skip-broken handling.
- The differences to preserve are that OAuth plugin loading does not mutate tool registrations or skill roots, only loads `oauth_module_path`, requires `register_oauth_providers(settings, runtime_paths)`, and coerces returned values to `OAuthProvider`.

3. Tool-service collision scanning is related to, but not a duplicate of, dynamic tool registry conflict checks.

- `src/mindroom/oauth/registry.py:158` rejects OAuth provider service names that overlap registered tool names, with a special allowance for `tool_config_service` when the tool metadata `auth_provider` matches the OAuth provider id.
- `src/mindroom/mcp/registry.py:166` and `src/mindroom/mcp/registry.py:192` reject MCP tool names that collide with existing tool metadata or registry entries.
- Both protect global tool/service namespaces, but the OAuth function checks service roles and auth-provider ownership rather than direct tool-name ownership.
- This is related validation, not a strong dedupe candidate.

## Proposed Generalization

1. Extract a private helper in `src/mindroom/oauth/registry.py`, for example `_load_oauth_provider_registry(config, runtime_paths, cache_key, *, skip_broken_plugins)`, that owns the duplicated cache-read, plugin-provider load, builtin merge, `_provider_registry`, cache-write, logging, and return path.
2. Keep `load_oauth_providers` and `load_oauth_providers_for_snapshot` responsible only for deriving the `Config`, `RuntimePaths`, and cache key.
3. Consider a later focused plugin-loader helper only if another plugin extension point appears; a generic iterator such as `plugin_imports._load_plugin_extensions(..., materialize=...)` would need to preserve current rollback semantics in `load_plugins`, so it is not worth doing for this OAuth-only duplication.

## Risk/tests

- The local loader helper has low behavioral risk if cache keys and lock boundaries are preserved exactly.
- Tests should cover cache hits for both direct config and snapshot paths, snapshot config hydration when `runtime_config` is absent, and duplicate provider/service validation still raising `PluginValidationError`.
- The broader plugin traversal generalization has higher risk because `load_plugins` snapshots and restores tool registry state around plugin materialization, while OAuth provider loading is read-only except module import caching.
