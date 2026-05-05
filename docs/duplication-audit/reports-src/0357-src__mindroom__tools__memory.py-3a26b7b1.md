## Summary

`src/mindroom/tools/memory.py` duplicates the metadata-only registration pattern used by other context-injected custom tools.
The duplication is small and intentional today: these tools require agent/runtime context and therefore cannot use the normal factory decorator without additional metadata-system support.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-30	duplicate-found	register_builtin_tool_metadata ToolMetadata metadata-only context injected custom tools memory delegate self_config compact_context	src/mindroom/tools/delegate.py:1; src/mindroom/tools/self_config.py:1; src/mindroom/tools/compact_context.py:1; src/mindroom/tools/attachments.py:19; src/mindroom/tool_system/metadata.py:760; src/mindroom/tool_system/registry_state.py:160
```

## Findings

### Metadata-only registration for context-injected custom tools

- `src/mindroom/tools/memory.py:1` registers a metadata-only built-in tool by constructing `ToolMetadata` and calling `register_builtin_tool_metadata`.
- `src/mindroom/tools/delegate.py:1`, `src/mindroom/tools/self_config.py:1`, and `src/mindroom/tools/compact_context.py:1` use the same module-level behavior: document that the actual toolkit is injected directly in `create_agent()`, then register a `ToolMetadata` entry with no factory, `setup_type=SetupType.NONE`, `status=ToolStatus.AVAILABLE`, and empty `config_fields`/`dependencies`.
- `src/mindroom/tool_system/registry_state.py:160` explicitly supports this metadata-only behavior by storing the metadata and removing any factory-backed registry entry when `metadata.factory` is absent.

Why this is functionally duplicated: each module exists only to make a context-bound custom toolkit visible in tool metadata/UI while keeping it out of the generic `TOOL_REGISTRY`.
The per-tool differences are the metadata literals: name, display name, description, category, icon, and icon color.

Differences to preserve: `self_config` uses `ToolCategory.DEVELOPMENT`; the others audited here use `ToolCategory.PRODUCTIVITY`.
Each tool has distinct user-facing copy and icon styling.

### Related-only: factory-backed metadata decorator already covers a neighboring case

- `src/mindroom/tools/attachments.py:19` uses `register_tool_with_metadata` for a custom toolkit that has managed initialization arguments and an actual factory.
- `src/mindroom/tool_system/metadata.py:760` builds `ToolMetadata` from keyword arguments and calls `register_builtin_tool_metadata` for built-in tools at `src/mindroom/tool_system/metadata.py:837`.

This is related, not a direct duplicate of `memory.py`, because `memory.py` deliberately avoids registering a generic toolkit factory.
It does show that a shared registration helper could fit naturally beside the existing decorator if metadata-only context-bound tools become more numerous.

## Proposed Generalization

No immediate refactor recommended for this 30-line module alone.

If the project wants to reduce this repeated pattern across all metadata-only context-injected tools, add a small helper in `mindroom.tool_system.metadata`, for example `register_context_tool_metadata(...)`, that accepts the varying metadata literals and internally calls `register_builtin_tool_metadata(ToolMetadata(..., config_fields=[], dependencies=[]))`.
Keep it metadata-only and do not add a factory argument.

## Risk/tests

Behavior risk is low but visible: an incorrect helper could accidentally add these tools to `TOOL_REGISTRY` or change UI metadata.
Tests should assert that `memory`, `delegate`, `self_config`, and `compact_context` are present in `TOOL_METADATA` and absent from `TOOL_REGISTRY`, and that their display metadata remains unchanged.
