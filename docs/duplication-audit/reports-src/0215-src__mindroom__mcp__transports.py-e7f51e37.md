## Summary

Top duplication candidates: MCP transport runtime shape checks in `src/mindroom/mcp/transports.py` overlap with transport-specific validation in `src/mindroom/mcp/config.py`, and the SSE and streamable HTTP openers repeat the same remote-transport wrapping pattern inside the primary module.
No broad refactor is recommended because the duplicated checks are defensive and the repeated remote opener code has only two transport-specific call sites.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MCPTransportHandle	class	lines 32-36	none-found	MCPTransportHandle deferred transport opener dataclass opener transport	src/mindroom/mcp/manager.py:262; src/mindroom/mcp/types.py:24; src/mindroom/hooks/context.py:1
_interpolate_value	function	lines 39-43	none-found	interpolate env placeholders ${ runtime_paths env_value regex substitute	src/mindroom/constants.py:222; src/mindroom/credentials_sync.py:50; src/mindroom/matrix/provisioning.py:16
_interpolate_value.<locals>.replace	nested_function	lines 40-41	none-found	regex replace callback env_value default empty string placeholder substitution	none
interpolate_mcp_env	function	lines 46-48	related-only	MCP env interpolation mapping env_value placeholders stdio env	src/mindroom/tool_system/dependencies.py:80; src/mindroom/credentials_sync.py:50; src/mindroom/workers/runtime.py:105
interpolate_mcp_headers	function	lines 51-53	related-only	MCP headers interpolation mapping env_value placeholders HTTP headers	src/mindroom/oauth/providers.py:124; src/mindroom/api/auth.py:106; src/mindroom/matrix/provisioning.py:16
build_stdio_server_parameters	function	lines 56-75	related-only	stdio server parameters command args env cwd default environment require command	src/mindroom/mcp/config.py:93; src/mindroom/mcp/manager.py:261; src/mindroom/mcp/registry.py:116
_open_stdio	async_function	lines 79-84	related-only	stdio_client asynccontextmanager build stdio server parameters transport opener	src/mindroom/mcp/manager.py:261; src/mindroom/mcp/config.py:93; src/mindroom/mcp/transports.py:121
_open_sse	async_function	lines 88-101	duplicate-found	sse_client asynccontextmanager headers timeout read timeout require url remote transport opener	src/mindroom/mcp/transports.py:105; src/mindroom/mcp/config.py:104; src/mindroom/mcp/manager.py:261
_open_streamable_http	async_function	lines 105-118	duplicate-found	streamablehttp_client asynccontextmanager headers timeout read timeout require url remote transport opener	src/mindroom/mcp/transports.py:88; src/mindroom/mcp/config.py:104; src/mindroom/mcp/manager.py:261
build_transport_handle	function	lines 121-137	related-only	transport dispatch stdio sse streamable-http opener unsupported transport	src/mindroom/mcp/config.py:121; src/mindroom/mcp/manager.py:262; src/mindroom/mcp/registry.py:117
```

## Findings

### 1. Remote MCP opener behavior is duplicated between SSE and streamable HTTP

`_open_sse` in `src/mindroom/mcp/transports.py:88` and `_open_streamable_http` in `src/mindroom/mcp/transports.py:105` both validate that `server_config.url` is present, interpolate configured headers, pass `startup_timeout_seconds` as the transport timeout, pass `call_timeout_seconds` as the SSE read timeout, enter an async MCP client context, and yield read/write streams.
The differences to preserve are the concrete client function, the transport-specific error message, and the stream tuple shape from `streamablehttp_client`, where the third returned item is intentionally dropped.

### 2. Transport required-field checks overlap with MCP config validation

`build_stdio_server_parameters` in `src/mindroom/mcp/transports.py:56` raises when `command` is missing.
`_open_sse` in `src/mindroom/mcp/transports.py:88` and `_open_streamable_http` in `src/mindroom/mcp/transports.py:105` raise when `url` is missing.
The same authored-config invariants are already enforced by `MCPServerConfig._validate_stdio_transport` in `src/mindroom/mcp/config.py:93` and `MCPServerConfig._validate_remote_transport` in `src/mindroom/mcp/config.py:104`.

Why this is duplicated: both layers protect MCP transport construction from impossible config states.
Differences to preserve: config validation rejects empty or whitespace-only strings and incompatible fields at parse time, while the transport layer has narrower `None` checks that act as defensive runtime guards and produce shorter transport-specific errors.

## Proposed Generalization

No production refactor is required for this audit.

If the remote opener duplication grows beyond the current two call sites, introduce a small private helper in `src/mindroom/mcp/transports.py`, for example `_open_remote_transport(server_config, runtime_paths, *, transport_name, client)`, with a tiny adapter for streamable HTTP's three-item result.
Keep the helper private and avoid moving the config validation checks out of `MCPServerConfig`.

For the required-field overlap with `MCPServerConfig`, no refactor is recommended.
The runtime guards are cheap defensive checks and the model validator remains the source of truth for authored config validation.

## Risk/tests

Risk is low if no code changes are made.
If the remote opener helper is introduced later, tests should cover SSE and streamable HTTP header interpolation, timeout propagation, missing URL errors, and streamable HTTP tuple truncation.
If transport guards are removed later, tests should verify all callers construct `MCPServerConfig` instances through Pydantic validation and never bypass model validation.

## Questions or Assumptions

Assumption: this is a report-only audit, so production code was not edited.
