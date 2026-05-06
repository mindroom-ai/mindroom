# Summary

Top duplication candidate: dynamic tool registry reconciliation is implemented for plugin tools in `src/mindroom/tool_system/registry_state.py` and for MCP tools in `src/mindroom/mcp/registry.py`.
Both paths derive desired tool factory/metadata maps, guard against name collisions, then mutate the shared `TOOL_METADATA` and `_TOOL_REGISTRY` globals.

No other meaningful duplication was found for the primary file.
Most symbols are narrow state primitives for plugin registration scoping, transactional snapshots, or one-off validation errors.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ToolMetadataValidationError	class	lines 32-33	related-only	ToolMetadataValidationError PluginValidationError ValidationError	src/mindroom/tool_system/plugin_imports.py:25; src/mindroom/tool_system/metadata.py:507; src/mindroom/config/main.py:946
_ToolRegistrySnapshot	class	lines 37-44	related-only	registry snapshot hook registry snapshot PreparedPluginReload	src/mindroom/tool_system/plugins.py:77; src/mindroom/hooks/registry.py:121; src/mindroom/hooks/context.py:151
_clear_plugin_tool_registrations	function	lines 47-49	none-found	clear plugin tool registrations plugin metadata by module	none
_snapshot_plugin_tool_registrations	function	lines 52-54	none-found	snapshot plugin tool registrations registrations.copy	src/mindroom/tool_system/plugins.py:367
_restore_plugin_tool_registrations	function	lines 57-65	none-found	restore plugin tool registrations failed reload	src/mindroom/tool_system/plugins.py:383
_scoped_plugin_registration_store	function	lines 69-82	related-only	scoped registration store contextmanager threading.local ContextVar reset	src/mindroom/ai_runtime.py:112; src/mindroom/tool_system/worker_routing.py:115; src/mindroom/tool_system/runtime_context.py:590
_scoped_plugin_registration_owner	function	lines 86-97	related-only	scoped registration owner contextmanager threading.local ContextVar reset	src/mindroom/ai_runtime.py:112; src/mindroom/tool_system/worker_routing.py:115; src/mindroom/tool_system/runtime_context.py:590
_plugin_registration_store	function	lines 100-105	none-found	active plugin registration sink registrations_by_module	none
_locked_tool_registry_state	function	lines 109-112	related-only	locked registry state RLock contextmanager shared state lock	src/mindroom/sync_bridge_state.py:18; src/mindroom/matrix/cache/write_coordinator.py:879
_resolved_tool_state	function	lines 115-138	duplicate-found	resolved tool state dynamic registry metadata factory collision resolved_mcp_tool_state	src/mindroom/mcp/registry.py:192; src/mindroom/mcp/registry.py:214
_synchronize_plugin_tools	function	lines 141-150	duplicate-found	synchronize plugin tools sync tool registry desired_registry desired_metadata	src/mindroom/mcp/registry.py:192; src/mindroom/mcp/registry.py:203
_reject_plugin_builtin_tool_collision	function	lines 153-157	duplicate-found	reject collision conflicts with built-in existing registered tool	src/mindroom/mcp/registry.py:197; src/mindroom/mcp/registry.py:199
register_builtin_tool_metadata	function	lines 160-170	related-only	register builtin tool metadata register_tool_with_metadata TOOL_METADATA _TOOL_REGISTRY	src/mindroom/tool_system/metadata.py:749; src/mindroom/tools/self_config.py:17; src/mindroom/tools/compact_context.py:17; src/mindroom/tools/memory.py:17; src/mindroom/tools/delegate.py:17
_register_plugin_tool_metadata	function	lines 173-180	related-only	register plugin tool metadata duplicate hook registration	src/mindroom/hooks/registry.py:50; src/mindroom/hooks/registry.py:61; src/mindroom/tool_system/metadata.py:824
_capture_tool_registry_snapshot	function	lines 183-200	related-only	capture tool registry snapshot sys.modules MODULE_IMPORT_CACHE validation module snapshot	src/mindroom/tool_system/plugins.py:138; src/mindroom/tool_system/plugins.py:228; src/mindroom/tool_system/metadata.py:879
_restore_tool_registry_snapshot	function	lines 203-225	related-only	restore tool registry snapshot clear update sys.modules module cache	src/mindroom/tool_system/plugins.py:147; src/mindroom/tool_system/plugins.py:241; src/mindroom/tool_system/metadata.py:898
```

# Findings

## Dynamic tool registry reconciliation is duplicated

`src/mindroom/tool_system/registry_state.py:115` builds a desired plugin overlay by copying built-in tool metadata and factories, checking plugin-vs-plugin conflicts, applying plugin metadata, and treating metadata without a factory as metadata-only.
`src/mindroom/tool_system/registry_state.py:141` then clears and replaces the live shared tool registry and metadata maps.

`src/mindroom/mcp/registry.py:192` performs the same category of behavior for configured MCP tools.
It computes desired registry/metadata maps through `resolved_mcp_tool_state`, rejects collisions with non-MCP tool names, unregisters stale MCP tools, writes desired entries into `TOOL_METADATA` and `_TOOL_REGISTRY`, and updates `_MCP_TOOL_NAMES`.

The behavior is not identical, but the overlap is real: both modules own dynamic tool namespaces layered onto the same global registry and both need collision detection plus a controlled mutation of `TOOL_METADATA` and `_TOOL_REGISTRY`.
Differences to preserve:

- Plugin tools overlay built-ins and can include metadata-only entries with no factory.
- Plugin collisions are checked against built-ins and across active plugins.
- MCP tools are an additional dynamic namespace tracked by `_MCP_TOOL_NAMES` and must unregister stale MCP entries without clearing unrelated tools.
- MCP currently raises `ValueError`, while plugin registration raises `ToolMetadataValidationError`.

# Proposed Generalization

Extract a small shared helper in `src/mindroom/tool_system/registry_state.py`, not a new abstraction layer.
For example, a private helper could apply a dynamic namespace to `TOOL_METADATA` and `_TOOL_REGISTRY` while accepting:

- desired factory map
- desired metadata map
- owned tool names from the previous sync
- a collision candidate set or predicate
- an exception factory/message callback

Keep `_resolved_tool_state` plugin-specific and keep MCP server config resolution in `src/mindroom/mcp/registry.py`.
Only the final collision-and-apply mechanics should be shared.

Suggested refactor plan:

1. Add a private helper near `_synchronize_plugin_tools` that reconciles owned dynamic tool names against desired registry/metadata maps.
2. Use it from `sync_mcp_tool_registry` first, because MCP already tracks owned names explicitly.
3. If the helper stays simple, optionally use it from `_synchronize_plugin_tools`; otherwise leave plugin synchronization as-is.
4. Preserve existing exception types and message text unless tests intentionally update them.
5. Add focused tests for stale MCP removal, MCP collision detection, plugin conflict detection, and metadata-only plugin entries.

# Risk/tests

Risk is moderate because both paths mutate global process registries used throughout tool loading and config validation.
The biggest risk is accidentally deleting built-in or plugin-owned entries while reconciling MCP tools, or changing plugin metadata-only behavior.

Tests should cover:

- plugin tool name conflict between two active plugins
- plugin tool name conflict with a built-in
- metadata-only built-in and plugin entries do not leave stale factories
- MCP dynamic tool sync removes stale MCP entries while preserving non-MCP entries
- MCP collision with existing non-MCP registry entries

No refactor is required for the scoped registration context managers or snapshot helpers.
Those are local mechanisms with only related patterns elsewhere, not duplicated behavior that would benefit from consolidation.
