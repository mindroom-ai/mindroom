Summary: No meaningful duplication found.
`src/mindroom/mcp/types.py` defines one custom async reader/writer lock and MCP-specific runtime value/state containers.
The closest related code is ordinary single-lock runtime state in other modules and MCP consumers in `mcp.manager`, `mcp.toolkit`, and `mcp.registry`, but there is no duplicated behavior worth extracting from this file.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
AsyncReadWriteLock	class	lines 22-62	none-found	AsyncReadWriteLock; asyncio.Condition; _active_readers; _waiting_writers; notify_all; read/write lock	src/mindroom/mcp/types.py:22; src/mindroom/workers/backends/kubernetes.py:146; src/mindroom/workers/backends/kubernetes.py:178; src/mindroom/workers/backends/kubernetes.py:227
AsyncReadWriteLock.__init__	method	lines 25-29	none-found	asyncio.Condition; _active_readers; _writer_active; _waiting_writers	src/mindroom/mcp/types.py:25; src/mindroom/runtime_support.py:31; src/mindroom/mcp/manager.py:48; src/mindroom/matrix/cache/postgres_event_cache.py:363
AsyncReadWriteLock.read	async_method	lines 32-44	none-found	asynccontextmanager read; active_readers; waiting_writers; condition.wait	src/mindroom/mcp/types.py:32; src/mindroom/matrix/client_session.py:100; src/mindroom/matrix/typing.py:47; src/mindroom/matrix/cache/sqlite_event_cache.py:331; src/mindroom/matrix/cache/postgres_event_cache.py:590
AsyncReadWriteLock.write	async_method	lines 47-62	none-found	asynccontextmanager write; writer_active; active_readers; notify_all	src/mindroom/mcp/types.py:47; src/mindroom/matrix/cache/sqlite_event_cache.py:356; src/mindroom/matrix/cache/postgres_event_cache.py:616; src/mindroom/matrix/cache/write_coordinator.py:47
MCPDiscoveredTool	class	lines 66-74	related-only	MCPDiscoveredTool; remote_name; function_name; input_schema; output_schema	src/mindroom/mcp/types.py:66; src/mindroom/mcp/manager.py:341; src/mindroom/mcp/toolkit.py:70; src/mindroom/mcp/toolkit.py:93; src/mindroom/tool_system/metadata.py:744
MCPServerCatalog	class	lines 78-88	related-only	MCPServerCatalog; catalog_hash; server_info; discovered_at; tool_prefix	src/mindroom/mcp/types.py:78; src/mindroom/mcp/manager.py:65; src/mindroom/mcp/manager.py:304; src/mindroom/mcp/manager.py:363; src/mindroom/mcp/toolkit.py:49; src/mindroom/mcp/registry.py:135
MCPServerState	class	lines 92-111	related-only	MCPServerState; server_id; config; session; exit_stack; semaphore; refresh_revision	src/mindroom/mcp/types.py:92; src/mindroom/mcp/manager.py:47; src/mindroom/mcp/manager.py:97; src/mindroom/custom_tools/claude_agent.py:99; src/mindroom/knowledge/refresh_runner.py:98; src/mindroom/matrix/cache/postgres_event_cache.py:334
MCPServerState.__post_init__	method	lines 109-111	none-found	max_concurrent_calls; asyncio.Semaphore; __post_init__ semaphore	src/mindroom/mcp/types.py:109; src/mindroom/mcp/manager.py:106; src/mindroom/knowledge/manager.py:1620
```

Findings:
No real duplicated behavior was identified for this primary file.

`AsyncReadWriteLock` is the only async reader/writer coordination primitive found under `src`.
Other condition-variable code in `src/mindroom/workers/backends/kubernetes.py:146`, `src/mindroom/workers/backends/kubernetes.py:178`, and `src/mindroom/workers/backends/kubernetes.py:227` is synchronous thread coordination around Kubernetes stream readers and does not share the same reader/writer semantics.
Other async context managers in Matrix cache/session modules wrap resource lifetimes or database access rather than reader/writer exclusion.

`MCPDiscoveredTool` and `MCPServerCatalog` are MCP-specific records consumed by `src/mindroom/mcp/manager.py:341`, `src/mindroom/mcp/manager.py:363`, `src/mindroom/mcp/toolkit.py:70`, and `src/mindroom/mcp/registry.py:135`.
These are related call sites, not competing representations of the same data.

`MCPServerState` resembles other runtime state holders that contain an `asyncio.Lock`, such as `src/mindroom/custom_tools/claude_agent.py:99`, `src/mindroom/knowledge/refresh_runner.py:98`, and `src/mindroom/matrix/cache/postgres_event_cache.py:334`.
Those classes track different resources and lifecycle fields, so extracting a common state abstraction would add indirection without removing duplicated behavior.

Proposed generalization: No refactor recommended.

Risk/tests: No code changes were made.
If the lock is changed later, focused async concurrency tests should cover concurrent readers, writer exclusion, and writer priority while a writer is waiting.
If MCP catalog/state shape changes later, tests around `MCPServerManager._discover_catalog`, `MindRoomMCPToolkit._register_catalog_tools`, and dynamic registry metadata should verify the serialized function names and catalog hash behavior.
