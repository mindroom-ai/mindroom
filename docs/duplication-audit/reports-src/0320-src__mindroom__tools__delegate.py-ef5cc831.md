Summary: `src/mindroom/tools/delegate.py` repeats the same metadata-only registration shape used by other context-bound built-in tools, especially `memory`, `self_config`, and `compact_context`.
The duplication is small and factual, but broadening it now would mostly replace clear declarative module bodies with an extra helper.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-30	duplicate-found	register_builtin_tool_metadata ToolMetadata metadata-only delegate custom_tools context-bound no generic factory	src/mindroom/tools/memory.py:1; src/mindroom/tools/self_config.py:1; src/mindroom/tools/compact_context.py:1; src/mindroom/tools/dynamic_tools.py:1; src/mindroom/tool_system/registry_state.py:160; src/mindroom/tool_system/metadata.py:800; src/mindroom/tools/__init__.py:19
```

## Findings

### Metadata-only built-in tool registration pattern

- `src/mindroom/tools/delegate.py:1` registers metadata for a context-bound custom toolkit without adding a generic factory to `TOOL_REGISTRY`.
- `src/mindroom/tools/memory.py:1`, `src/mindroom/tools/self_config.py:1`, and `src/mindroom/tools/compact_context.py:1` use the same module-level behavior: import `SetupType`, `ToolCategory`, `ToolMetadata`, `ToolStatus`, and `register_builtin_tool_metadata`, then register a `ToolMetadata` object with no `factory`, empty `config_fields`, and empty `dependencies`.
- `src/mindroom/tool_system/registry_state.py:160` confirms why these modules work the same way: `register_builtin_tool_metadata()` stores metadata but removes registry entries when `metadata.factory` is absent.
- `src/mindroom/tools/__init__.py:19` imports these metadata-only modules for their registration side effect, including `delegate`.

Why this is duplicated: the files are all representing built-in tools whose actual toolkit instances require runtime or agent context and are injected elsewhere, so each file repeats the same side-effect-only metadata registration boilerplate.

Differences to preserve:

- Tool identity fields differ: `name`, `display_name`, `description`, `category`, `icon`, and `icon_color`.
- `dynamic_tools` is related but uses direct `TOOL_METADATA["dynamic_tools"] = ToolMetadata(...)` assignment in `src/mindroom/tools/dynamic_tools.py:8`, so it is a nearby inconsistency rather than an exact duplicate of the `delegate.py` pattern.

## Proposed Generalization

No refactor recommended for `delegate.py` alone.
If this pattern grows, the minimal helper would be a metadata-only registration wrapper in `mindroom.tool_system.metadata`, such as `register_metadata_only_builtin_tool(...)`, accepting the variable metadata fields and defaulting `status=ToolStatus.AVAILABLE`, `setup_type=SetupType.NONE`, `config_fields=[]`, and `dependencies=[]`.
That helper could then replace `delegate`, `memory`, `self_config`, and `compact_context`; `dynamic_tools` could be converted separately to the existing `register_builtin_tool_metadata()` form first.

## Risk/tests

- Behavior risk is low but nonzero because these modules rely on import side effects for UI metadata visibility.
- Tests should verify `TOOL_METADATA` contains `delegate`, `memory`, `self_config`, and `compact_context` after importing `mindroom.tools`, while `TOOL_REGISTRY` does not contain metadata-only tools without factories.
- No production code was edited for this audit.
