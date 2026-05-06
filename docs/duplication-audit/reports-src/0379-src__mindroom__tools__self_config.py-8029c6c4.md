## Summary

`src/mindroom/tools/self_config.py` duplicates the metadata-only registration pattern used by other context-injected built-in tools.
The closest duplicates are `src/mindroom/tools/memory.py`, `src/mindroom/tools/delegate.py`, and `src/mindroom/tools/compact_context.py`, which all import the same metadata symbols and call `register_builtin_tool_metadata(ToolMetadata(...))` without registering a generic toolkit factory.
`src/mindroom/tools/dynamic_tools.py` is related, but it writes directly to `TOOL_METADATA` instead of using `register_builtin_tool_metadata`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-30	duplicate-found	register_builtin_tool_metadata ToolMetadata metadata-only NOT added TOOL_REGISTRY self_config memory delegate compact_context dynamic_tools	src/mindroom/tools/memory.py:1-30; src/mindroom/tools/delegate.py:1-30; src/mindroom/tools/compact_context.py:1-30; src/mindroom/tools/dynamic_tools.py:1-21; src/mindroom/tool_system/registry_state.py:160-170; src/mindroom/tools/__init__.py:19-25
```

## Findings

### Metadata-only tool registration modules repeat the same behavior

`src/mindroom/tools/self_config.py:1-30` is a metadata-only registration module.
It documents that the actual toolkit is instantiated directly in `create_agent()`, imports `SetupType`, `ToolCategory`, `ToolMetadata`, `ToolStatus`, and `register_builtin_tool_metadata`, then registers a `ToolMetadata` object with `config_fields=[]` and `dependencies=[]`.

The same behavior appears in:

- `src/mindroom/tools/memory.py:1-30`
- `src/mindroom/tools/delegate.py:1-30`
- `src/mindroom/tools/compact_context.py:1-30`

These modules differ only in tool identity, display text, category, icon, and color.
The behavior is otherwise the same: expose built-in metadata for UI/catalog visibility while intentionally avoiding a generic toolkit factory.
That behavior is supported directly by `register_builtin_tool_metadata()` in `src/mindroom/tool_system/registry_state.py:160-170`, which stores metadata and removes registry entries when no factory is present.

`src/mindroom/tools/dynamic_tools.py:1-21` is related but not an exact duplicate.
It also registers metadata for a context-injected tool, but it writes to `TOOL_METADATA` directly, which bypasses the durable built-in metadata registry path used by `self_config`, `memory`, `delegate`, and `compact_context`.
That difference should be preserved or intentionally normalized only after checking plugin overlay behavior.

## Proposed Generalization

A small helper could reduce the repeated metadata-only registration modules:

`mindroom.tool_system.metadata.register_metadata_only_builtin_tool(...)`

It would accept the fields that vary (`name`, `display_name`, `description`, `category`, `icon`, `icon_color`) and set the repeated defaults (`status=ToolStatus.AVAILABLE`, `setup_type=SetupType.NONE`, `config_fields=[]`, `dependencies=[]`) before calling `register_builtin_tool_metadata`.

No refactor is required for this file alone.
The duplication is real but small, and the current explicit modules are easy to read.
If touched later, include `dynamic_tools` only if its direct `TOOL_METADATA` write is confirmed to be unnecessary.

## Risk/tests

Main risk is accidentally registering a factory or changing whether metadata-only tools appear in plugin-resolved catalogs and UI metadata.
Tests should cover that `self_config`, `memory`, `delegate`, and `compact_context` exist in `TOOL_METADATA` but not in `_TOOL_REGISTRY`, and that plugin synchronization keeps these built-in metadata entries visible.
If `dynamic_tools` is normalized to use `register_builtin_tool_metadata`, add coverage proving it remains visible wherever session-scoped dynamic tool loading expects it.
