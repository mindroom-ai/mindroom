## Summary

No meaningful duplication found.
`ensure_tool_registry_loaded` is the central mutable bootstrap helper for importing built-in tools, loading plugin tools, and syncing MCP dynamic tools.
Other source locations either call this helper directly or implement related but intentionally different plugin reload and runtime snapshot flows.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ensure_tool_registry_loaded	function	lines 12-28	related-only	ensure_tool_registry_loaded; import mindroom.tools; load_plugins; sync_mcp_tool_registry; resolved_tool_state_for_runtime	src/mindroom/config/main.py:1158; src/mindroom/config/main.py:1405; src/mindroom/agents.py:631; src/mindroom/api/sandbox_runner.py:235; src/mindroom/tool_system/plugins.py:114; src/mindroom/tool_system/metadata.py:915; src/mindroom/orchestrator.py:747; src/mindroom/mcp/registry.py:192
```

## Findings

No real duplicated behavior found for the complete bootstrap sequence in `src/mindroom/tool_system/bootstrap.py:12`.

Related candidates checked:

- `src/mindroom/config/main.py:1158` and `src/mindroom/config/main.py:1405` lazily import and call `ensure_tool_registry_loaded` before resolving worker-routed tools or authored tool overrides.
  These are consumers, not duplicates.
- `src/mindroom/agents.py:631` calls `ensure_tool_registry_loaded` before deriving default worker-routed tools.
  This is another consumer of the central helper.
- `src/mindroom/api/sandbox_runner.py:235` wraps `ensure_tool_registry_loaded` in `ensure_registry_loaded_with_config`.
  The wrapper adds sandbox-runner naming/context but does not duplicate the underlying behavior.
- `src/mindroom/tool_system/plugins.py:114` imports `mindroom.tools` and loads plugin tools, but it does not sync MCP dynamic tools and also owns skill-root behavior.
  This is a narrower primitive used by the bootstrap helper.
- `src/mindroom/mcp/registry.py:192` reconciles MCP dynamic tool registry entries only.
  This is another narrower primitive used by the bootstrap helper.
- `src/mindroom/tool_system/metadata.py:915` computes a non-mutating runtime registry snapshot from built-ins, plugins, and MCP state.
  It is related in domain but intentionally differs from `ensure_tool_registry_loaded`, which mutates the live global registry.
- `src/mindroom/orchestrator.py:747` builds the hook registry by loading plugins with normal skill-root updates.
  This overlaps with plugin loading but has lifecycle behavior that should remain separate from the catalog/bootstrap helper.

## Proposed Generalization

No refactor recommended.
The existing helper is already the minimal generalization for live mutable tool-registry bootstrap.

## Risk/tests

Risk is low if left unchanged.
Any future refactor should preserve these distinctions:

- `ensure_tool_registry_loaded` must keep importing built-in tools before registry lookups.
- Plugin loading from bootstrap intentionally passes `set_skill_roots=False`.
- MCP registry sync must remain part of live registry bootstrap.
- Snapshot builders such as `resolved_tool_state_for_runtime` must remain non-mutating.

Relevant tests would be focused around worker-routed tool defaults, sandbox runner startup, plugin tool registration, and MCP tool visibility.
