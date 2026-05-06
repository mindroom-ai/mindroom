## Summary

No meaningful duplication found.
The hook registry compile path in `src/mindroom/hooks/registry.py` is a focused source of truth for converting discovered plugin hook callbacks into immutable `RegisteredHook` tuples.
The closest related code is plugin materialization in `src/mindroom/tool_system/plugins.py`, OAuth provider registry validation in `src/mindroom/oauth/registry.py`, and hook execution lookup in `src/mindroom/hooks/execution.py`, but these do not duplicate the registry's behavior enough to justify a shared abstraction.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
HookRegistryPlugin	class	lines 22-28	related-only	HookRegistryPlugin discovered_hooks entry_config plugin_order Protocol _Plugin	src/mindroom/tool_system/plugins.py:53; src/mindroom/tool_system/plugins.py:60; src/mindroom/tool_system/plugins.py:65; src/mindroom/tool_system/plugins.py:335
_callback_source_lineno	function	lines 31-32	none-found	_callback_source_lineno co_firstlineno getsourcefile getsourcelines inspect source_lineno	src/mindroom/hooks/registry.py:86; src/mindroom/hooks/types.py:160; src/mindroom/hooks/execution.py:230; src/mindroom/tool_system/plugins.py:87; src/mindroom/hooks/decorators.py:46
HookRegistry	class	lines 36-118	related-only	HookRegistry hooks_for has_hooks from_plugins Registry HookRegistry.empty	src/mindroom/hooks/execution.py:230; src/mindroom/tool_system/plugins.py:101; src/mindroom/tool_system/plugins.py:219; src/mindroom/orchestrator.py:747; src/mindroom/scheduling.py:74; src/mindroom/tool_system/runtime_context.py:88
HookRegistry.empty	method	lines 42-44	related-only	HookRegistry.empty default_factory empty registry deactivate_plugins	src/mindroom/tool_system/plugins.py:108; src/mindroom/orchestrator.py:263; src/mindroom/scheduling.py:74; src/mindroom/tool_system/runtime_context.py:88; src/mindroom/bot.py:306
HookRegistry.from_plugins	method	lines 47-110	related-only	from_plugins discovered_hooks get_hook_metadata entry_config.hooks unknown_overrides duplicate hook registration plugin_order sorted priority source_lineno	src/mindroom/tool_system/plugins.py:233; src/mindroom/tool_system/plugins.py:347; src/mindroom/hooks/decorators.py:72; src/mindroom/oauth/registry.py:112; src/mindroom/mcp/manager.py:311; src/mindroom/agents.py:401
HookRegistry.hooks_for	method	lines 112-114	related-only	hooks_for registry lookup _hooks_by_event get event_name _eligible_hooks	src/mindroom/hooks/execution.py:235; src/mindroom/hooks/execution.py:344; src/mindroom/mcp/manager.py:53; src/mindroom/mcp/manager.py:65
HookRegistry.has_hooks	method	lines 116-118	related-only	has_hooks hook_registry emit guard EVENT_ src/mindroom/hooks/execution.py:235; src/mindroom/bot.py:716; src/mindroom/turn_policy.py:120; src/mindroom/delivery_gateway.py:105; src/mindroom/tool_system/runtime_context.py:563; src/mindroom/mcp/manager.py:53
HookRegistryState	class	lines 122-125	related-only	HookRegistryState registry mutable holder state hook_registry_state	src/mindroom/bot.py:306; src/mindroom/hooks/context.py:147; src/mindroom/hooks/context.py:152; src/mindroom/api/config_lifecycle.py:70; src/mindroom/runtime_state.py:10
```

## Findings

No real duplication found.

The most similar behavior is the plugin discovery handoff in `src/mindroom/tool_system/plugins.py:335`.
That function loads plugin modules, discovers decorated hooks with `iter_module_hooks`, and stores them on `_Plugin`.
`HookRegistry.from_plugins` then applies hook-specific override filtering, duplicate `(plugin_name, hook_name)` suppression, unknown override warnings, `RegisteredHook` construction, and deterministic event ordering.
Those are adjacent phases of one pipeline, not duplicated implementations.

`src/mindroom/oauth/registry.py:112` has a registry-building loop with duplicate provider detection.
It is only conceptually related because it builds a mapping and tracks duplicates.
The duplicate policy and data model differ: OAuth keeps the latest provider while tracking duplicate IDs and service collisions, while hook compilation skips duplicate hook registrations per plugin and sorts hooks by priority, plugin order, and source line.

`HookRegistry.hooks_for` and `HookRegistry.has_hooks` are used repeatedly as the intended registry API.
The guards in `src/mindroom/bot.py:716`, `src/mindroom/turn_policy.py:120`, `src/mindroom/delivery_gateway.py:105`, and `src/mindroom/tool_system/runtime_context.py:563` are call-site checks, not duplicated lookup implementations.

## Proposed Generalization

No refactor recommended.

The registry currently centralizes hook compilation and lookup.
Extracting a generic plugin registry builder would need parameters for duplicate policy, override handling, source ordering, warning messages, and output model construction.
That would add abstraction without reducing active duplicated behavior.

## Risk/Tests

No production code was changed.
If this area is refactored later, tests should cover duplicate hook names within a plugin, disabled hook overrides, priority and timeout overrides, unknown override warnings, event sorting by `(priority, plugin_order, source_lineno)`, empty registry behavior, and `has_hooks`/`hooks_for` results for missing events.
