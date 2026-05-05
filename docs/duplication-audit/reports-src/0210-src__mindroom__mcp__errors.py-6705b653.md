Summary: No meaningful duplication found.
`src/mindroom/mcp/errors.py` defines a compact MCP-specific exception hierarchy with a shared `server_id` context field.
Other modules contain similar typed exception patterns, but they are domain-specific and do not duplicate MCP behavior.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MCPError	class	lines 6-11	related-only	class .*Error RuntimeError, super().__init__(message), server_id, provider_id, failure_kind	src/mindroom/oauth/providers.py:38, src/mindroom/oauth/providers.py:50, src/mindroom/api/sandbox_worker_prep.py:68, src/mindroom/streaming_delivery.py:41
MCPError.__init__	method	lines 9-11	related-only	super().__init__(message), self.server_id, self.provider_id, self.failure_kind	src/mindroom/oauth/providers.py:53, src/mindroom/api/sandbox_worker_prep.py:71, src/mindroom/streaming_delivery.py:44
MCPConnectionError	class	lines 14-15	none-found	MCPConnectionError, ConnectionError, not connected, operation failed	src/mindroom/mcp/manager.py:73, src/mindroom/mcp/manager.py:187, src/mindroom/mcp/manager.py:206, src/mindroom/mcp/manager.py:650, src/mindroom/cli/main.py:182
MCPTimeoutError	class	lines 18-19	related-only	MCPTimeoutError, TimeoutError, timed out, shutdown timeout	src/mindroom/mcp/manager.py:291, src/mindroom/mcp/manager.py:649, src/mindroom/streaming_delivery.py:49, src/mindroom/history/compaction.py:106
MCPProtocolError	class	lines 22-23	none-found	MCPProtocolError, ProtocolError, invalid, inconsistent, duplicate function name	src/mindroom/mcp/manager.py:336, src/mindroom/mcp/manager.py:339, src/mindroom/mcp/manager.py:466, src/mindroom/matrix/client_delivery.py:86
MCPToolCallError	class	lines 26-27	none-found	MCPToolCallError, ToolCallError, tool call failed, isError	src/mindroom/mcp/results.py:93, src/mindroom/mcp/manager.py:162
```

Findings:

No real duplication found.

The closest related pattern is a domain-specific base exception that stores extra structured context after calling `super().__init__(message)`.
`MCPError` stores `server_id` in `src/mindroom/mcp/errors.py:9`, `OAuthConnectionRequired` stores `provider_id` and `connect_url` in `src/mindroom/oauth/providers.py:53`, `WorkerRequestPreparationError` stores `failure_kind` in `src/mindroom/api/sandbox_worker_prep.py:71`, and `_NonTerminalDeliveryError` stores the original exception in `src/mindroom/streaming_delivery.py:44`.
These are structurally similar but not functionally duplicated because each one carries different domain context, uses different base exception types, and is consumed by different workflows.

The MCP subclasses are marker exception types used by `src/mindroom/mcp/manager.py` and `src/mindroom/mcp/results.py`.
`MCPConnectionError` represents unavailable MCP server/session states at `src/mindroom/mcp/manager.py:73`, `src/mindroom/mcp/manager.py:187`, and `src/mindroom/mcp/manager.py:206`.
`MCPTimeoutError` is raised for MCP startup/runtime timeout wrapping at `src/mindroom/mcp/manager.py:291` and `src/mindroom/mcp/manager.py:649`.
`MCPProtocolError` is raised for invalid MCP function names, duplicate exposed names, and missing cached tools at `src/mindroom/mcp/manager.py:336`, `src/mindroom/mcp/manager.py:339`, and `src/mindroom/mcp/manager.py:466`.
`MCPToolCallError` is raised only when a tool result reports `isError` in `src/mindroom/mcp/results.py:93`.
Those distinctions are active behavior and should remain separate.

Proposed generalization: No refactor recommended.

A shared contextual-exception base class would add abstraction without removing meaningful duplicated behavior.
Keeping `MCPError` local to `mindroom.mcp` preserves clear domain semantics and keeps downstream `except` handling straightforward.

Risk/tests:

No production change is recommended, so there is no migration risk.
If this hierarchy is changed later, tests should cover `MCPManager.call_tool` retry behavior for connection and timeout errors, protocol-error propagation without reconnect, and `raise_for_mcp_call_error` raising `MCPToolCallError` with the original `server_id`.
