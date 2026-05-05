## Summary

Top duplication candidate: MCP remote-tool include/exclude filtering is implemented in both `src/mindroom/mcp/toolkit.py` and `src/mindroom/mcp/manager.py`.
Related validation/normalization logic for MCP tool filters exists in `src/mindroom/mcp/config.py` and `src/mindroom/mcp/registry.py`, but it applies at different lifecycle points and should not be merged into the toolkit without preserving those boundaries.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
bind_mcp_server_manager	function	lines 19-22	related-only	bind_mcp_server_manager require_mcp_server_manager mcp_server_manager global manager	src/mindroom/orchestrator.py:685; src/mindroom/orchestrator.py:698; src/mindroom/mcp/registry.py:118; tests/test_mcp_registry.py:45; tests/test_mcp_integration_fake_server.py:72
require_mcp_server_manager	function	lines 25-27	related-only	require_mcp_server_manager active runtime manager MCP toolkit factory	src/mindroom/mcp/registry.py:118; src/mindroom/mcp/registry.py:148
normalize_tool_name_filter	function	lines 30-38	related-only	normalize_tool_name_filter normalize_tool_filters include_tools exclude_tools strip split comma	src/mindroom/mcp/config.py:69; src/mindroom/mcp/registry.py:100; tests/test_mcp_config.py:50; tests/test_dynamic_toolkits.py:332
MindRoomMCPToolkit	class	lines 41-109	related-only	MindRoomMCPToolkit Toolkit auto_register async_functions Function MCPDiscoveredTool	src/mindroom/mcp/registry.py:141; src/mindroom/tool_system/toolkit_aliases.py:14; tests/test_mcp_toolkit.py:45
MindRoomMCPToolkit.__init__	method	lines 44-68	related-only	MindRoomMCPToolkit __init__ include_tools exclude_tools call_timeout_seconds catalog manager	src/mindroom/mcp/registry.py:141; src/mindroom/mcp/config.py:61; src/mindroom/mcp/manager.py:148; tests/test_mcp_toolkit.py:49
MindRoomMCPToolkit._filtered_tools	method	lines 70-82	duplicate-found	include_tools exclude_tools remote_name filtered_tools catalog.tools	src/mindroom/mcp/manager.py:320; src/mindroom/mcp/config.py:85; src/mindroom/mcp/registry.py:100; tests/test_mcp_toolkit.py:68
MindRoomMCPToolkit._register_catalog_tools	method	lines 84-91	related-only	Duplicate MCP function name function_names seen async_functions	src/mindroom/mcp/manager.py:324; src/mindroom/mcp/manager.py:337; src/mindroom/mcp/manager.py:469; tests/test_mcp_toolkit.py:95
MindRoomMCPToolkit._build_function	method	lines 93-109	none-found	Function entrypoint skip_entrypoint_processing manager.call_tool remote_name timeout_seconds	src/mindroom/mcp/manager.py:131; src/mindroom/tool_system/toolkit_aliases.py:31; tests/test_mcp_toolkit.py:45
MindRoomMCPToolkit._build_function.<locals>._call_tool	nested_async_function	lines 94-101	none-found	_call_tool manager.call_tool server_id remote_name dict kwargs timeout_seconds	src/mindroom/mcp/manager.py:131; src/mindroom/tool_system/tool_hooks.py:390; tests/test_mcp_toolkit.py:63
```

## Findings

### Duplicate remote-tool filtering

`src/mindroom/mcp/toolkit.py:70` filters cached `MCPDiscoveredTool` records by `include_tools` and `exclude_tools`, using `tool.remote_name` as the match key.
`src/mindroom/mcp/manager.py:320` performs the same allowlist/denylist control flow while discovering the server catalog, using raw MCP `tool.name` before converting it into `MCPDiscoveredTool.remote_name`.

The behavior is duplicated because both sites:

- Convert include/exclude lists to sets.
- Drop excluded remote tool names first.
- Apply the include allowlist only when it is non-empty.
- Preserve original order for matching tools.

Differences to preserve:

- The manager filters server-level config before function-name validation, hashing, and catalog construction.
- The toolkit filters an already cached catalog for per-assignment overrides and returns existing `MCPDiscoveredTool` objects.
- The manager uses `tool.name`; the toolkit uses `tool.remote_name`.

### Related but distinct filter normalization and overlap validation

`src/mindroom/mcp/toolkit.py:30` normalizes runtime override values from either a list or a comma/newline-delimited string.
`src/mindroom/mcp/config.py:69` normalizes top-level MCP server config filters, but only accepts list input and raises on non-string list entries.
`src/mindroom/mcp/registry.py:95` validates normalized per-agent override payloads for include/exclude overlap and timeout values.

These are related, but not direct duplicates.
They operate at different stages: config parsing, authored override validation, and runtime toolkit construction.
Merging them casually would risk changing accepted runtime override shapes or Pydantic validation errors.

### Related duplicate function-name checks

`src/mindroom/mcp/toolkit.py:84` rejects duplicate function names inside one cached catalog before registering Agno async functions.
`src/mindroom/mcp/manager.py:324` rejects duplicate names during catalog discovery, and `src/mindroom/mcp/manager.py:469` checks collisions across servers and local tools.

This is related defensive validation rather than actionable duplication.
The toolkit check is a local invariant protecting `async_functions` assignment, while the manager checks protocol/catalog validity and cross-surface model-visible collisions.

## Proposed Generalization

If this duplication becomes worth reducing, add a tiny helper in `src/mindroom/mcp/config.py` or a new focused `src/mindroom/mcp/tool_filters.py`:

```python
def remote_tool_name_allowed(remote_name: str, include_tools: Collection[str], exclude_tools: Collection[str]) -> bool:
    if exclude_tools and remote_name in exclude_tools:
        return False
    return not include_tools or remote_name in include_tools
```

Then the manager and toolkit can each keep their own iteration and object construction while sharing only the boolean predicate.
No broader refactor is recommended for the manager/toolkit registration or `Function` wrapping paths.

## Risk/tests

Behavior risks:

- Changing filter matching could expose or hide MCP tools, especially when both server-level filters and per-assignment overrides are configured.
- Moving normalization into a shared helper could accidentally accept comma-delimited strings in top-level config, or reject strings currently accepted by runtime overrides.
- Removing the toolkit duplicate-name guard would make `async_functions` silently overwrite entries if a malformed catalog reaches the toolkit.

Tests that would need attention for any future refactor:

- `tests/test_mcp_toolkit.py:68` for per-assignment toolkit filtering.
- `tests/test_mcp_toolkit.py:95` for duplicate function-name rejection.
- `tests/test_mcp_config.py:39` and `tests/test_mcp_config.py:50` for config-level overlap validation and normalization.
- `tests/test_dynamic_toolkits.py:332` for authored override validation.
- MCP manager catalog discovery tests around `tests/test_mcp_manager.py:318` should continue to prove server-level filtering and catalog construction behavior.
