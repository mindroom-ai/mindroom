Summary: `src/mindroom/tools/dynamic_tools.py` duplicates the metadata-only registration pattern used by `delegate`, `self_config`, `compact_context`, and `memory`, but it writes directly to `TOOL_METADATA` instead of using `register_builtin_tool_metadata`.
This is a real behavioral difference because plugin/runtime metadata resolution is rebuilt from `_BUILTIN_TOOL_METADATA`, not only `TOOL_METADATA`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-21	duplicate-found	TOOL_METADATA["dynamic_tools"], ToolMetadata(, register_builtin_tool_metadata, metadata-only built-in tools, dynamic_tools	src/mindroom/tools/dynamic_tools.py:1; src/mindroom/tools/delegate.py:1; src/mindroom/tools/self_config.py:1; src/mindroom/tools/compact_context.py:1; src/mindroom/tools/memory.py:1; src/mindroom/tool_system/registry_state.py:115; src/mindroom/tool_system/registry_state.py:160; src/mindroom/tool_system/metadata.py:915
```

Findings:

1. Metadata-only built-in registration is duplicated but `dynamic_tools` uses a weaker path.
   `src/mindroom/tools/dynamic_tools.py:1` describes the same role as the metadata-only modules at `src/mindroom/tools/delegate.py:1`, `src/mindroom/tools/self_config.py:1`, `src/mindroom/tools/compact_context.py:1`, and `src/mindroom/tools/memory.py:1`: expose a custom/context-injected tool in metadata without registering a generic toolkit factory.
   The peer modules all call `register_builtin_tool_metadata(...)` at lines 17-30, while `dynamic_tools` assigns `TOOL_METADATA["dynamic_tools"] = ToolMetadata(...)` at `src/mindroom/tools/dynamic_tools.py:10`.
   `register_builtin_tool_metadata` stores metadata in both `_BUILTIN_TOOL_METADATA` and `TOOL_METADATA` at `src/mindroom/tool_system/registry_state.py:160`.
   Runtime/plugin overlays rebuild visible metadata from `_BUILTIN_TOOL_METADATA` in `_resolved_tool_state` at `src/mindroom/tool_system/registry_state.py:115` and in `resolved_tool_state_for_runtime` at `src/mindroom/tool_system/metadata.py:915`.
   That means `dynamic_tools` can be visible only in the mutable global `TOOL_METADATA` after import, while the other metadata-only built-ins are durable across registry rebuilds and runtime snapshots.
   Differences to preserve: `dynamic_tools` should still remain metadata-only and should still not add a generic factory to `TOOL_REGISTRY`.

Proposed generalization:

Use the existing `register_builtin_tool_metadata` helper in `src/mindroom/tools/dynamic_tools.py`, matching the peer metadata-only modules.
No new abstraction is recommended because the helper already expresses the shared behavior and correctly preserves metadata-only built-ins by omitting a factory.

Minimal refactor plan:

1. Replace the direct `TOOL_METADATA` import with `register_builtin_tool_metadata`.
2. Wrap the existing `ToolMetadata(...)` call in `register_builtin_tool_metadata(...)`.
3. Keep all metadata field values unchanged.
4. Add or update a focused registry/runtime metadata test that proves `dynamic_tools` remains visible after `resolved_tool_state_for_runtime` or plugin overlay rebuilds.

Risk/tests:

Behavior risk is low because this aligns `dynamic_tools` with existing metadata-only built-ins and should not register a factory.
Tests should cover metadata export/runtime resolution with no plugins and, if practical, after plugin state synchronization, because those paths read `_BUILTIN_TOOL_METADATA`.
