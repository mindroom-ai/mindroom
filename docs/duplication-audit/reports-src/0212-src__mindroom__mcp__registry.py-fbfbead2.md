## Summary

Top duplication candidates for `src/mindroom/mcp/registry.py`:

1. MCP include/exclude tool-filter overlap validation is repeated between per-agent MCP overrides and server-level MCP config validation.
2. MCP registry collision/merge logic is closely related to the central runtime tool-state merge path, but the two call sites preserve different mutation semantics and error types.

No literal duplicate code was found.
The duplication is behavioral and narrow.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
mcp_tool_name	function	lines 33-35	related-only	mcp_tool_name mcp_ prefix server_id tool name	src/mindroom/mcp/manager.py:77; src/mindroom/mcp/manager.py:365; src/mindroom/orchestrator.py:710; src/mindroom/orchestration/config_updates.py:180; src/mindroom/tool_system/worker_routing.py:320
mcp_server_id_from_tool_name	function	lines 38-46	related-only	mcp_server_id_from_tool_name removeprefix mcp_ factory marker	src/mindroom/mcp/manager.py:77; src/mindroom/mcp/manager.py:563; src/mindroom/tool_system/worker_routing.py:322
mcp_registry_tool_names	function	lines 49-51	none-found	mcp_registry_tool_names _MCP_TOOL_NAMES active dynamic MCP	names none
_registered_mcp_tool_names	function	lines 54-63	related-only	registered mcp tool names marker _TOOL_REGISTRY _MCP_TOOL_FACTORY_MARKER	src/mindroom/tool_system/registry_state.py:183; src/mindroom/tool_system/registry_state.py:203
_tool_override_fields	function	lines 66-92	related-only	ConfigField include_tools exclude_tools call_timeout_seconds agent_override_fields	src/mindroom/mcp/config.py:61; src/mindroom/mcp/config.py:64; src/mindroom/tools/shell.py:307
validate_mcp_agent_overrides	function	lines 95-112	duplicate-found	include_tools exclude_tools overlap call_timeout_seconds greater than 0 validation	src/mindroom/mcp/config.py:85; src/mindroom/tool_system/metadata.py:263; src/mindroom/tool_system/metadata.py:1219
_tool_metadata	function	lines 115-136	related-only	ToolMetadata function_names catalog tools authored_override_validator MCP	src/mindroom/tool_system/metadata.py:1032; src/mindroom/tools/shell.py:307; src/mindroom/api/tools.py:138
_tool_factory	function	lines 139-163	related-only	factory Toolkit nested class __name__ managed init args	src/mindroom/tools/shell.py:332; src/mindroom/tools/__init__.py:272; src/mindroom/mcp/toolkit.py:41
_tool_factory.<locals>.factory	nested_function	lines 140-160	related-only	nested factory returns type Toolkit BoundMindRoomMCPToolkit	src/mindroom/tools/shell.py:332; src/mindroom/tools/__init__.py:272; src/mindroom/tools/__init__.py:326
_tool_factory.<locals>.__init__	nested_function	lines 142-157	related-only	MindRoomMCPToolkit init manager catalog include_tools exclude_tools call_timeout_seconds	src/mindroom/mcp/toolkit.py:44; src/mindroom/mcp/toolkit.py:63; src/mindroom/tools/shell.py:338
register_mcp_tool	function	lines 166-174	related-only	register tool metadata registry conflict existing registered tool	src/mindroom/tool_system/registry_state.py:160; src/mindroom/tool_system/registry_state.py:173
unregister_mcp_tool	function	lines 177-181	related-only	pop TOOL_METADATA _TOOL_REGISTRY discard names restore registry	src/mindroom/tool_system/registry_state.py:203; src/mindroom/tool_system/registry_state.py:147
_desired_server_entries	function	lines 184-189	related-only	config.mcp_servers enabled server entries	src/mindroom/mcp/manager.py:87
sync_mcp_tool_registry	function	lines 192-211	duplicate-found	desired registry metadata collision reconcile update global registry	src/mindroom/tool_system/metadata.py:1002; src/mindroom/tool_system/registry_state.py:141; src/mindroom/tool_system/registry_state.py:203
resolved_mcp_tool_state	function	lines 214-224	related-only	resolved tool state runtime registry metadata without mutating globals	src/mindroom/tool_system/metadata.py:915; src/mindroom/tool_system/registry_state.py:115
```

## Findings

### 1. Repeated MCP include/exclude overlap validation

`src/mindroom/mcp/registry.py:95` validates per-agent MCP overrides by computing `sorted(set(include_tools) & set(exclude_tools))` and raising when the same remote tool appears in both lists.
`src/mindroom/mcp/config.py:85` performs the same include/exclude overlap check for server-level MCP config.

The behavior is the same core rule: MCP allowlists and denylists must not overlap.
The differences to preserve are the error messages and input shapes.
`MCPServerConfig._validate_tool_filters()` reads typed model fields and reports `MCP include_tools and exclude_tools overlap`.
`validate_mcp_agent_overrides()` reads normalized override dictionaries, includes the MindRoom tool name in the error, and also validates `call_timeout_seconds`.

### 2. MCP registry collision/merge behavior overlaps with runtime tool-state merge

`src/mindroom/mcp/registry.py:192` resolves desired MCP registry and metadata, checks for collisions against non-MCP global tool entries, removes stale MCP entries, writes desired entries, and updates `_MCP_TOOL_NAMES`.
`src/mindroom/tool_system/metadata.py:1002` performs the same collision rule when merging MCP state into resolved runtime tool state: detect any name collision between MCP registry/metadata and existing registry/metadata, then merge.

The shared behavior is MCP tool-name collision detection before registry/metadata insertion.
The differences to preserve are important.
`sync_mcp_tool_registry()` mutates process-global registries and must exclude existing MCP-owned entries via `_registered_mcp_tool_names()`.
`_merge_mcp_tool_state()` is pure over caller-provided dictionaries and raises `ToolMetadataValidationError` instead of `ValueError`.

## Proposed Generalization

For the filter validation, a tiny helper in `src/mindroom/mcp/config.py` or a focused MCP validation module could return the sorted overlap for two filter lists.
Both `MCPServerConfig._validate_tool_filters()` and `validate_mcp_agent_overrides()` could keep their current error messages while sharing the overlap calculation.

For the collision behavior, no refactor is recommended right now.
The repeated expression is small, and the mutation semantics and exception types differ enough that a helper would likely obscure the two call sites.

## Risk/tests

If the filter-overlap helper is introduced later, tests should cover server-level `include_tools`/`exclude_tools` overlap and per-agent MCP override overlap separately because the public error messages differ.
Existing tests around tool metadata validation should also verify `call_timeout_seconds` continues to reject bools, non-numbers, and non-positive values.

If collision logic is ever generalized, tests should cover both paths: global MCP registry sync with stale MCP entries present, and runtime metadata resolution with plugin or built-in name collisions.
