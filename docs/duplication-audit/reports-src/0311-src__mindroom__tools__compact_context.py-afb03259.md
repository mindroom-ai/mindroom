Summary: `src/mindroom/tools/compact_context.py` duplicates the metadata-only registration pattern used by other context-bound custom tools, especially `delegate`, `memory`, and `self_config`.
The duplicated behavior is small and intentional: these modules expose UI metadata for tools whose runtime toolkit is instantiated directly in `create_agent()` instead of through `TOOL_REGISTRY`.
No production refactor is recommended from this file alone because the repeated code is declarative, low-risk, and only around 30 lines per tool.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-30	duplicate-found	compact_context register_builtin_tool_metadata ToolMetadata metadata-only context-bound tools create_agent direct toolkit	delegate.py:1-30; memory.py:1-30; self_config.py:1-30; dynamic_tools.py:1-21; thread_summary.py:13-30; attachments.py:19-42; agents.py:532-553; custom_tools/compact_context.py:20-63
```

Findings:

1. Metadata-only custom-tool registration is repeated across context-bound tools.
   `src/mindroom/tools/compact_context.py:1-30`, `src/mindroom/tools/delegate.py:1-30`, `src/mindroom/tools/memory.py:1-30`, and `src/mindroom/tools/self_config.py:1-30` all import the same metadata symbols, construct a `ToolMetadata` object, set `status=ToolStatus.AVAILABLE`, `setup_type=SetupType.NONE`, `config_fields=[]`, and `dependencies=[]`, then register it for UI visibility.
   The functional intent is the same: register metadata for a built-in tool whose concrete toolkit requires runtime agent/session context and is wired directly in `src/mindroom/agents.py`.
   Differences to preserve are the actual metadata values: `name`, `display_name`, `description`, `category`, `icon`, and `icon_color`.

2. The "context-bound tool, direct create_agent instantiation" lifecycle is repeated with minor registration variants.
   `src/mindroom/agents.py:532-553` directly instantiates both `SelfConfigTools` and `CompactContextTools`, while `src/mindroom/tools/self_config.py:1-30` and `src/mindroom/tools/compact_context.py:1-30` register only metadata.
   `src/mindroom/tools/dynamic_tools.py:1-21` is the same lifecycle and comment pattern, but still writes directly to `TOOL_METADATA["dynamic_tools"]` rather than calling `register_builtin_tool_metadata`.
   `src/mindroom/tools/thread_summary.py:13-30` and `src/mindroom/tools/attachments.py:19-42` are related but not duplicates of this exact behavior because they use `register_tool_with_metadata` and provide factories.

Proposed generalization:

No refactor recommended for this audit scope.
If more metadata-only context-bound tools are added, consider a small helper in `mindroom.tool_system.metadata`, for example `register_context_bound_builtin_tool_metadata(...)`, that fills the common `AVAILABLE`, `NONE`, empty config fields, and empty dependencies defaults while still requiring explicit tool-specific metadata.
The helper should not handle factory registration or direct toolkit construction.

Risk/tests:

Changing this pattern would affect tool visibility in the dashboard and any code that reads `TOOL_METADATA`.
Tests should assert that `compact_context`, `delegate`, `memory`, `self_config`, and `dynamic_tools` remain present in metadata and are not accidentally added to the generic `TOOL_REGISTRY`.
Because this task is report-only and no production code was edited, no runtime tests were run.
