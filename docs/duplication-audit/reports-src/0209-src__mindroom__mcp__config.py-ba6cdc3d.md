## Summary

Top duplication candidate: MCP tool filter normalization and include/exclude overlap validation is repeated across server config parsing, MCP toolkit runtime overrides, authored override normalization, and per-agent override validation.
Identifier validation, transport field validation, and tool-prefix resolution are mostly MCP-specific or have only related defensive checks elsewhere.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
validate_mcp_identifier	function	lines 16-25	related-only	validate identifier regex alphanumeric underscores plugin name agent names normalize key part	src/mindroom/tool_system/plugin_identity.py:10; src/mindroom/config/main.py:447; src/mindroom/tool_system/worker_routing.py:162
validate_mcp_function_name	function	lines 28-39	none-found	function name regex max length provider visible validate tool function name	src/mindroom/mcp/manager.py:331; src/mindroom/mcp/toolkit.py:84; src/mindroom/tool_system/metadata.py:1173
normalize_mcp_server_id	function	lines 42-44	related-only	normalize server id mcp server id validate entity names	src/mindroom/config/main.py:460; src/mindroom/mcp/registry.py:33; src/mindroom/tool_system/worker_routing.py:386
MCPServerConfig	class	lines 47-133	related-only	MCPServerConfig transport command args url headers include_tools exclude_tools BaseModel transport config	src/mindroom/mcp/transports.py:56; src/mindroom/mcp/registry.py:95; src/mindroom/config/models.py:122
MCPServerConfig.normalize_tool_filters	method	lines 71-83	duplicate-found	normalize tool filters include_tools exclude_tools string array override comma newline strip list strings	src/mindroom/mcp/toolkit.py:30; src/mindroom/tool_system/metadata.py:1173; src/mindroom/config/knowledge.py:78
MCPServerConfig._validate_tool_filters	method	lines 85-91	duplicate-found	include_tools exclude_tools overlap allowlist denylist overlap	src/mindroom/mcp/registry.py:95; src/mindroom/mcp/toolkit.py:70
MCPServerConfig._validate_stdio_transport	method	lines 93-102	related-only	stdio transport require command do not allow url headers build stdio command required	src/mindroom/mcp/transports.py:56; src/mindroom/mcp/transports.py:121
MCPServerConfig._validate_remote_transport	method	lines 104-119	related-only	sse streamable-http require url do not allow command args cwd env remote transport	src/mindroom/mcp/transports.py:87; src/mindroom/mcp/transports.py:104; src/mindroom/mcp/transports.py:121
MCPServerConfig.validate_transport_fields	method	lines 122-133	related-only	model_validator transport fields tool_prefix include exclude transport dispatch	src/mindroom/mcp/transports.py:121; src/mindroom/config/main.py:446; src/mindroom/config/models.py:244
resolved_mcp_tool_prefix	function	lines 136-139	none-found	resolved tool prefix mcp tool_prefix server_id prefix validate identifier function name prefix	src/mindroom/mcp/manager.py:320; src/mindroom/mcp/registry.py:33; src/mindroom/mcp/toolkit.py:55
```

## Findings

### 1. MCP tool filter normalization is duplicated

`MCPServerConfig.normalize_tool_filters` in `src/mindroom/mcp/config.py:71` normalizes `include_tools` and `exclude_tools` by accepting lists, requiring each item to be a string, stripping whitespace, and dropping empty entries.
`normalize_tool_name_filter` in `src/mindroom/mcp/toolkit.py:30` performs the same stripping and empty-entry removal for runtime include/exclude filters, with the extra legacy behavior of accepting comma/newline-separated strings and returning `None` for empty filters.
`_normalize_string_array_override` in `src/mindroom/tool_system/metadata.py:1173` also normalizes string-array config overrides from either a list or comma/newline-separated string and enforces string entries.

Why this is duplicated: all three functions canonicalize authored or runtime string-list filters before later matching against tool names.
Differences to preserve: config model fields currently keep empty filters as `[]`, while toolkit and override normalization return `None`; config model fields reject non-list values, while override paths accept legacy strings.

### 2. MCP include/exclude overlap validation is duplicated

`MCPServerConfig._validate_tool_filters` in `src/mindroom/mcp/config.py:85` rejects overlap between server-level `include_tools` and `exclude_tools`.
`validate_mcp_agent_overrides` in `src/mindroom/mcp/registry.py:95` repeats the same set intersection check for per-agent MCP overrides, with a different error prefix.

Why this is duplicated: both enforce the same invariant for MCP allowlist and denylist filters at different configuration layers.
Differences to preserve: server config reports `MCP include_tools and exclude_tools overlap`, while per-agent overrides include the tool name and override context in the error.

## Proposed Generalization

Add a small MCP-local helper in `src/mindroom/mcp/config.py` or a focused `src/mindroom/mcp/tool_filters.py` module, for example:

1. `normalize_mcp_tool_filter(value, *, allow_legacy_string: bool, empty_as_none: bool)`.
2. `validate_mcp_tool_filter_overlap(include_tools, exclude_tools, *, subject: str)`.
3. Use the helper from `MCPServerConfig`, `MindRoomMCPToolkit`, and `validate_mcp_agent_overrides`.
4. Keep wrapper functions or parameters for the existing `[]` versus `None` behavior.
5. Update existing MCP config/toolkit/registry tests that assert exact normalization or error text.

No refactor is recommended for identifier validation, transport validation, or prefix resolution.
Those behaviors are either intentionally stricter for MCP naming or are defensive runtime checks after Pydantic validation.

## Risk/tests

Risk is low if the helper remains MCP-local and preserves current `[]` versus `None` return semantics.
The main behavior risk is changing accepted legacy string handling for runtime/authored overrides or changing exact validation error messages.
Tests should cover `MCPServerConfig` include/exclude normalization, `MindRoomMCPToolkit` string and list filters, `_normalize_string_array_override` for MCP authored overrides, and `validate_mcp_agent_overrides` overlap rejection.

## Questions or Assumptions

Assumption: this audit is report-only, per the task instruction to avoid production code edits.
