Summary: The main duplication candidates are the in-memory keyed resource lifecycle in `ClaudeAgentTools` versus browser profiles, static sandbox workers, shell background handles, and credential leases.
These modules repeat keyed state lookup, touch/update timestamps, stale cleanup, close/evict behavior, and status formatting, but their resource semantics differ enough that a shared abstraction is only worth considering if another persistent SDK-backed tool is added.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_Agent	class	lines 39-42	related-only	agent protocol name id model runtime context	src/mindroom/custom_tools/delegate.py:95; src/mindroom/custom_tools/subagents.py:613; src/mindroom/tool_system/runtime_context.py:557
_AgentWithId	class	lines 46-49	related-only	agent id protocol agent_id target_agent	src/mindroom/custom_tools/delegate.py:95; src/mindroom/custom_tools/subagents.py:541; src/mindroom/custom_tools/subagents.py:613
_AgentWithModel	class	lines 53-56	related-only	agent model id resolve model	src/mindroom/api/openai_compat.py:720; src/mindroom/model_loading.py:116; src/mindroom/thread_summary.py:88
_ModelWithId	class	lines 60-63	related-only	model id protocol resolve model	src/mindroom/model_loading.py:116; src/mindroom/thread_summary.py:88; src/mindroom/cli/doctor.py:309
_RunContext	class	lines 66-69	related-only	run context session_id resolve_current_session_id	src/mindroom/tool_system/runtime_context.py:55; src/mindroom/custom_tools/compact_context.py:42; src/mindroom/api/openai_compat.py:720
_parse_csv_list	function	lines 72-75	none-found	comma-separated tools split strip allowed_tools disallowed_tools	none
_normalize_permission_mode	function	lines 78-84	none-found	permission_mode acceptEdits bypassPermissions normalize permission	none
_parse_int	function	lines 87-90	related-only	clamp max minimum ttl sessions max	int src/mindroom/api/sandbox_worker_prep.py:81; src/mindroom/api/sandbox_worker_prep.py:86; src/mindroom/tool_system/sandbox_proxy.py:143
_parse_optional_int	function	lines 93-96	related-only	optional int clamp minimum max_turns limit offset	src/mindroom/custom_tools/subagents.py:645; src/mindroom/custom_tools/subagents.py:646; src/mindroom/api/sandbox_worker_prep.py:86
_ClaudeSessionState	class	lines 100-113	duplicate-found	keyed resource dataclass created_at last_used_at lock stderr	src/mindroom/workers/backends/static_runner.py:29; src/mindroom/tools/shell.py:233; src/mindroom/custom_tools/browser.py:251
_ClaudeSessionManager	class	lines 116-228	duplicate-found	in-memory manager sessions get_or_create close cleanup evict keyed resources	src/mindroom/custom_tools/browser.py:251; src/mindroom/workers/backends/static_runner.py:42; src/mindroom/api/sandbox_worker_prep.py:91
_ClaudeSessionManager.__init__	method	lines 119-122	duplicate-found	dict lock keyed resource state namespace limits	src/mindroom/custom_tools/browser.py:254; src/mindroom/workers/backends/static_runner.py:47; src/mindroom/api/sandbox_worker_prep.py:55
_ClaudeSessionManager.configure_namespace	method	lines 124-133	related-only	configure per namespace ttl max sessions clamp	src/mindroom/tools/shell.py:226; src/mindroom/matrix_identifiers.py:13; src/mindroom/workers/backends/static_runner.py:52
_ClaudeSessionManager.get_or_create	async_method	lines 135-166	duplicate-found	get existing or create keyed resource connect touch cleanup	src/mindroom/custom_tools/browser.py:993; src/mindroom/workers/backends/static_runner.py:60; src/mindroom/api/sandbox_worker_prep.py:98
_ClaudeSessionManager.close	async_method	lines 168-175	duplicate-found	close remove keyed resource disconnect	src/mindroom/custom_tools/browser.py:1017; src/mindroom/tools/shell.py:520; src/mindroom/workers/backends/static_runner.py:127
_ClaudeSessionManager.get	async_method	lines 177-188	duplicate-found	get keyed resource cleanup expired touch	src/mindroom/workers/backends/static_runner.py:99; src/mindroom/workers/backends/static_runner.py:108; src/mindroom/api/sandbox_worker_prep.py:122
_ClaudeSessionManager._collect_expired_locked	method	lines 190-198	duplicate-found	remove expired keyed entries under lock	src/mindroom/api/sandbox_worker_prep.py:91; src/mindroom/tools/shell.py:398; src/mindroom/workers/backends/static_runner.py:147
_ClaudeSessionManager._evict_if_needed_locked	method	lines 200-213	duplicate-found	evict oldest max active sessions namespace last_used_at	src/mindroom/workers/backends/static_runner.py:127; src/mindroom/tools/shell.py:429; src/mindroom/custom_tools/subagents.py:654
_ClaudeSessionManager._namespace_ttl_seconds	method	lines 215-216	related-only	namespace ttl lookup default	src/mindroom/mcp/config.py:66; src/mindroom/api/sandbox_worker_prep.py:37; src/mindroom/tool_system/sandbox_proxy.py:143
_ClaudeSessionManager._namespace_max_sessions	method	lines 218-219	related-only	namespace max sessions lookup default	src/mindroom/config/memory.py:52; src/mindroom/config/memory.py:57; src/mindroom/api/sandbox_worker_prep.py:86
_ClaudeSessionManager._disconnect	async_method	lines 221-224	duplicate-found	close resource suppress exception under resource lock	src/mindroom/custom_tools/browser.py:1017; src/mindroom/api/openai_compat.py:1700; src/mindroom/runtime_support.py:248
_ClaudeSessionManager._disconnect_many	async_method	lines 226-228	duplicate-found	close many resources sequentially	src/mindroom/custom_tools/browser.py:264; src/mindroom/api/openai_compat.py:1700; src/mindroom/runtime_support.py:248
ClaudeAgentTools	class	lines 231-627	related-only	toolkit persistent sessions status interrupt end send	src/mindroom/custom_tools/browser.py:251; src/mindroom/tools/shell.py:375; src/mindroom/custom_tools/subagents.py:519
ClaudeAgentTools.__init__	method	lines 236-283	related-only	toolkit init registered tools config fields	src/mindroom/custom_tools/browser.py:254; src/mindroom/custom_tools/subagents.py:497; src/mindroom/tools/shell.py:257
ClaudeAgentTools._build_options	method	lines 285-317	none-found	ClaudeAgentOptions env api key allowed disallowed cli_path	none
ClaudeAgentTools._build_stderr_callback	method	lines 320-328	related-only	deque stderr callback strip append bounded lines	src/mindroom/tools/shell.py:417; src/mindroom/tools/shell.py:422; src/mindroom/custom_tools/browser.py:607
ClaudeAgentTools._build_stderr_callback.<locals>._on_stderr	nested_function	lines 323-326	related-only	strip nonempty append deque callback	src/mindroom/tools/shell.py:417; src/mindroom/tools/shell.py:422
ClaudeAgentTools._format_session_error	method	lines 330-354	related-only	format diagnostic error context stderr recent lines	src/mindroom/tools/shell.py:481; src/mindroom/custom_tools/matrix_api.py:170; src/mindroom/api/openai_compat.py:543
ClaudeAgentTools._namespace	method	lines 356-366	related-only	resolve namespace from agent id/name fallback	src/mindroom/tools/shell.py:226; src/mindroom/matrix_identifiers.py:13; src/mindroom/cli/connect.py:156
ClaudeAgentTools._ensure_namespace_config	method	lines 368-373	related-only	ensure namespace limits before session lookup	src/mindroom/runtime_support.py:87; src/mindroom/workers/backends/static_runner.py:52
ClaudeAgentTools._session_key	method	lines 375-386	duplicate-found	derive session key from namespace run session label	src/mindroom/api/openai_compat.py:720; src/mindroom/custom_tools/subagents.py:210; src/mindroom/tools/shell.py:226
ClaudeAgentTools._resolve_model	method	lines 388-400	related-only	resolve configured model else agent model id	src/mindroom/api/openai_compat.py:720; src/mindroom/model_loading.py:116; src/mindroom/cli/doctor.py:309
ClaudeAgentTools._get_or_create_session	async_method	lines 402-455	duplicate-found	validate options derive key build resource create format error	src/mindroom/custom_tools/browser.py:993; src/mindroom/workers/backends/static_runner.py:60; src/mindroom/custom_tools/subagents.py:591
ClaudeAgentTools.claude_start_session	async_method	lines 457-477	related-only	start or reuse session return status	src/mindroom/custom_tools/subagents.py:591; src/mindroom/custom_tools/browser.py:353; src/mindroom/workers/backends/static_runner.py:60
ClaudeAgentTools.claude_send	async_method	lines 479-529	related-only	validate nonempty prompt send persistent session collect response	src/mindroom/custom_tools/subagents.py:519; src/mindroom/tools/shell.py:375; src/mindroom/api/openai_compat.py:1260
ClaudeAgentTools._collect_response	async_method	lines 531-558	none-found	Claude SDK receive_response TextBlock ToolUseBlock ResultMessage	none
ClaudeAgentTools._format_response_output	method	lines 560-572	related-only	format text cost tools used unique sorted	src/mindroom/tools/shell.py:481; src/mindroom/api/openai_compat.py:1307; src/mindroom/tool_system/tool_calls.py:552
ClaudeAgentTools.claude_session_status	async_method	lines 574-595	duplicate-found	status for keyed resource active age idle id	src/mindroom/custom_tools/browser.py:489; src/mindroom/tools/shell.py:485; src/mindroom/workers/backends/static_runner.py:118
ClaudeAgentTools.claude_interrupt	async_method	lines 597-614	related-only	interrupt stop active resource by key	src/mindroom/tools/shell.py:520; src/mindroom/custom_tools/browser.py:581; src/mindroom/response_lifecycle.py:160
ClaudeAgentTools.claude_end_session	async_method	lines 616-627	duplicate-found	close/remove active keyed resource by session key	src/mindroom/custom_tools/browser.py:1017; src/mindroom/workers/backends/static_runner.py:127; src/mindroom/tools/shell.py:520
```

Findings:

1. Keyed in-memory resource managers are repeated across persistent Claude sessions, browser profiles, static sandbox workers, shell handles, and credential leases.
`_ClaudeSessionManager` keeps a dict of keyed state, protects it with a lock, creates or reuses resources, updates `last_used_at`, removes stale entries, evicts by namespace/session limit, and closes removed resources in `src/mindroom/custom_tools/claude_agent.py:116`.
The same behavior shape appears in `BrowserTools._profiles` with lock-protected create/stop flow in `src/mindroom/custom_tools/browser.py:254`, `BrowserTools._ensure_profile` in `src/mindroom/custom_tools/browser.py:993`, and `BrowserTools._stop_profile` in `src/mindroom/custom_tools/browser.py:1017`.
It also appears in `StaticSandboxRunnerBackend._workers` with `ensure_worker`, `touch_worker`, `evict_worker`, and `cleanup_idle_workers` in `src/mindroom/workers/backends/static_runner.py:47`, `src/mindroom/workers/backends/static_runner.py:60`, `src/mindroom/workers/backends/static_runner.py:108`, `src/mindroom/workers/backends/static_runner.py:127`, and `src/mindroom/workers/backends/static_runner.py:147`.
Credential leases repeat the lock-protected dict plus expiry cleanup flow in `src/mindroom/api/sandbox_worker_prep.py:55`, `src/mindroom/api/sandbox_worker_prep.py:91`, `src/mindroom/api/sandbox_worker_prep.py:98`, and `src/mindroom/api/sandbox_worker_prep.py:122`.
Differences to preserve: Claude sessions are async, per-session locked, and must not evict active client locks; browser profiles need Playwright context shutdown; static workers mark some evictions idle instead of deleting; credential leases consume uses and raise HTTP errors.

2. Session/resource key derivation and scoped lookup are repeated.
`ClaudeAgentTools._namespace` and `_session_key` derive an owner namespace from agent identity plus run-context session and optional label in `src/mindroom/custom_tools/claude_agent.py:356` and `src/mindroom/custom_tools/claude_agent.py:375`.
OpenAI compatibility derives namespaced session IDs from auth headers, request headers, model, and fallback UUID in `src/mindroom/api/openai_compat.py:720`.
Sub-agent tools parse and record Matrix room/thread session keys in `src/mindroom/custom_tools/subagents.py:210`, `src/mindroom/custom_tools/subagents.py:362`, and `src/mindroom/custom_tools/subagents.py:389`.
Shell tools derive a namespace from storage root and base directory for handle ownership in `src/mindroom/tools/shell.py:226`.
These are functionally similar because each prevents cross-context reuse of long-lived state, but the inputs and security boundaries differ.

3. Human-readable status/start/stop command flows are similar across tools.
Claude sessions expose start/reuse, status, interrupt, and close in `src/mindroom/custom_tools/claude_agent.py:457`, `src/mindroom/custom_tools/claude_agent.py:574`, `src/mindroom/custom_tools/claude_agent.py:597`, and `src/mindroom/custom_tools/claude_agent.py:616`.
Shell background commands expose run/background, poll status, and kill in `src/mindroom/tools/shell.py:375`, `src/mindroom/tools/shell.py:485`, and `src/mindroom/tools/shell.py:520`.
Browser tools expose status, tabs, open/focus/close, and profile stop in `src/mindroom/custom_tools/browser.py:489`, `src/mindroom/custom_tools/browser.py:540`, `src/mindroom/custom_tools/browser.py:550`, `src/mindroom/custom_tools/browser.py:565`, `src/mindroom/custom_tools/browser.py:581`, and `src/mindroom/custom_tools/browser.py:1017`.
The duplication is mostly behavioral UI shape, not implementation-level enough to justify a shared command framework.

Proposed generalization:

No immediate refactor recommended for this file alone.
If another persistent SDK-backed tool is introduced, add a small focused helper such as `src/mindroom/tool_system/keyed_resource_manager.py` that owns only generic lock-protected keyed state, TTL collection, max-entry eviction, and close-many orchestration.
Keep resource-specific behavior outside the helper through typed callbacks for create, close, active/evictable checks, and timestamp extraction.
Do not merge OpenAI session IDs, shell namespaces, and Claude session keys into one helper; those encode different trust boundaries.
Optionally add a tiny local helper for nonempty string normalization before touching this file again, but only if nearby code gains more than the current two or three call sites.

Risk/tests:

The biggest risk in deduplicating the manager is changing resource lifetime semantics, especially active Claude session locks, browser context shutdown order, worker idle preservation, and credential lease one-use consumption.
Tests would need focused coverage for TTL expiry, max-session eviction, active-session non-eviction, close/disconnect failure suppression, session key derivation with labels and run contexts, and user-visible status/error strings.
No production code was edited for this audit.
