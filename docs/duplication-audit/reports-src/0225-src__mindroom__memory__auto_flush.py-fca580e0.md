## Summary

Top duplication candidates:

1. `src/mindroom/memory/auto_flush.py` has local AgentSession coercion and loading helpers that duplicate the already-public `get_agent_session()` behavior in `src/mindroom/agent_storage.py`.
2. Auto-flush has bespoke `ToolExecutionIdentity` JSON serialization/deserialization that overlaps with knowledge refresh subprocess request serialization and hydration in `src/mindroom/knowledge/refresh_runner.py`.
3. Auto-flush state read/write uses the same atomic JSON-file persistence shape as published knowledge-index metadata, but the payloads and recovery semantics differ enough that this is related-only.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_FlushSessionEntry	class	lines 38-56	not-a-behavior-symbol	TypedDict flush session entry dirty in_flight last_flushed	none
_FlushState	class	lines 59-63	not-a-behavior-symbol	TypedDict flush state sessions version	none
_SerializedExecutionIdentity	class	lines 66-77	not-a-behavior-symbol	TypedDict execution identity channel requester room thread tenant account	src/mindroom/tool_system/worker_routing.py:52; src/mindroom/knowledge/refresh_runner.py:265
_state_path	function	lines 80-83	related-only	state path mkdir json state file metadata path	src/mindroom/knowledge/registry.py:343; src/mindroom/matrix/invited_rooms_store.py:55
_now_ts	function	lines 86-87	none-found	datetime now UTC timestamp int now_ts	none
_empty_state	function	lines 90-91	none-found	empty state version sessions	none
_session_key	function	lines 94-97	related-only	session key agent session worker key	src/mindroom/custom_tools/subagents.py:210; src/mindroom/thread_tags.py:336
_serialize_execution_identity	function	lines 100-111	duplicate-found	ToolExecutionIdentity asdict execution_identity json channel requester_id room_id thread_id tenant_id account_id	src/mindroom/knowledge/refresh_runner.py:265; src/mindroom/knowledge/refresh_runner.py:278
_deserialize_execution_identity	function	lines 114-147	duplicate-found	ToolExecutionIdentity payload optional string field channel agent_name requester_id room_id thread_id tenant_id account_id	src/mindroom/knowledge/refresh_runner.py:874; src/mindroom/knowledge/refresh_runner.py:884
_resolve_flush_scope	function	lines 150-166	related-only	resolve_agent_execution private worker_key execution_identity	src/mindroom/runtime_resolution.py:146; src/mindroom/tool_system/worker_routing.py:486
_sanitize_session_entry	function	lines 169-181	none-found	sanitize session entry execution_identity worker_key room_id thread_id	none
_stale_private_session_entry	function	lines 184-200	none-found	stale private session entry worker_key resolve_flush_scope	none
_read_state_unlocked	function	lines 203-228	related-only	read json state sanitize entries empty on invalid json	src/mindroom/knowledge/registry.py:280; src/mindroom/matrix/invited_rooms_store.py:32
_write_state_unlocked	function	lines 231-235	related-only	atomic json write tmp replace ensure_ascii indent	src/mindroom/knowledge/registry.py:318; src/mindroom/matrix/invited_rooms_store.py:55
_notify_workers	function	lines 238-240	related-only	wake events set notify workers condition notify_all	src/mindroom/mcp/types.py:44; src/mindroom/workers/backends/kubernetes.py:178
auto_flush_enabled	function	lines 243-245	none-found	auto_flush enabled uses_file_memory	none
_agent_uses_file_memory	function	lines 248-251	related-only	get_agent_memory_backend file use_file_memory_backend	src/mindroom/memory/functions.py:155; src/mindroom/memory/functions.py:213
mark_auto_flush_dirty_session	function	lines 254-297	none-found	mark dirty session first_dirty_at dirty_revision in_flight next_attempt_at	none
reprioritize_auto_flush_sessions	function	lines 300-335	none-found	reprioritize dirty sessions priority_boost_at max_cross_session_reprioritize	none
_coerce_agent_session	function	lines 338-344	duplicate-found	AgentSession from_dict get_session SessionType.AGENT coerce dict	src/mindroom/agent_storage.py:119
_load_agent_session	function	lines 347-362	duplicate-found	create_session_storage get_session SessionType.AGENT AgentSession from_dict	src/mindroom/agent_storage.py:71; src/mindroom/agent_storage.py:119
_entry_priority_key	function	lines 365-368	none-found	priority_boost_at first_dirty_at priority key	none
_flush_batch_key	function	lines 371-376	none-found	flush batch key private worker_key agent_name	none
_select_recent_chat_lines	function	lines 379-409	none-found	get_chat_history user assistant role clean truncate max chars recent lines	none
_normalize_extractor_line	function	lines 412-421	none-found	no_reply_token bullet numbered line normalize extractor output	none
_sanitize_extractor_output	function	lines 424-448	related-only	dedup output lines lower seen join pipe no_reply_token	src/mindroom/memory/_file_backend.py:246; src/mindroom/tools/shell.py:120
_build_existing_memory_context	async_function	lines 451-489	related-only	list_all_agent_memories memory snippets reverse truncate context	src/mindroom/custom_tools/memory.py:140; src/mindroom/memory/functions.py:238
_extract_memory_summary	async_function	lines 492-543	related-only	Agent arun prompt extract durable memory model_loading get_model_instance	src/mindroom/routing.py:101; src/mindroom/scheduling.py:699; src/mindroom/voice_handler.py:470
_retry_cooldown_seconds	function	lines 546-553	related-only	retry cooldown exponential max retry cooldown	src/mindroom/knowledge/utils.py:108; src/mindroom/knowledge/utils.py:176
MemoryAutoFlushWorker	class	lines 557-858	related-only	background worker stop event wake event run cycle process session flush	src/mindroom/orchestrator.py:290; src/mindroom/knowledge/refresh_scheduler.py:52
MemoryAutoFlushWorker.stop	method	lines 566-569	related-only	stop event wake event set graceful shutdown	src/mindroom/orchestrator.py:290
MemoryAutoFlushWorker.run	async_method	lines 571-587	related-only	async worker loop wait_for wake event timeout interval	src/mindroom/knowledge/refresh_scheduler.py:52
MemoryAutoFlushWorker._run_cycle	async_method	lines 589-694	none-found	stale ttl dirty items in_flight next_attempt batch per agent idle age	none
MemoryAutoFlushWorker._process_session_key	async_method	lines 696-805	none-found	process session key wait_for timeout failure cooldown dirty_revision requeue	none
MemoryAutoFlushWorker._flush_session	async_method	lines 807-858	none-found	flush session select recent chat extract summary append daily memory flush_marker	none
```

## Findings

### 1. Agent session loading is duplicated

`src/mindroom/memory/auto_flush.py:338` implements `_coerce_agent_session()` by accepting an `AgentSession`, accepting a `dict`, and calling `AgentSession.from_dict()`.
`src/mindroom/agent_storage.py:119` implements the same behavior in `get_agent_session()` after reading `storage.get_session(session_id, SessionType.AGENT)`.

`src/mindroom/memory/auto_flush.py:347` also creates session storage, reads `SessionType.AGENT`, and delegates to its local coercion helper.
That duplicates the combination of `create_session_storage()` at `src/mindroom/agent_storage.py:71` and `get_agent_session()` at `src/mindroom/agent_storage.py:119`.

Difference to preserve: auto-flush must pass the resolved private-agent `execution_identity` to `create_session_storage()`.
That can be preserved by keeping storage creation local and replacing only the raw read/coercion with `get_agent_session(storage, session_id)`.

### 2. Execution identity JSON serialization is repeated

`src/mindroom/memory/auto_flush.py:100` manually projects `ToolExecutionIdentity` fields into a JSON-safe dict.
`src/mindroom/knowledge/refresh_runner.py:265` serializes the same dataclass into a subprocess payload with `asdict(execution_identity)`.

`src/mindroom/memory/auto_flush.py:114` validates the persisted dict and rebuilds `ToolExecutionIdentity`.
`src/mindroom/knowledge/refresh_runner.py:874` and `src/mindroom/knowledge/refresh_runner.py:884` validate optional string fields and rebuild the same dataclass from a JSON payload.

Differences to preserve: auto-flush treats malformed persisted identities as absent and resets/sanitizes state, while knowledge refresh raises `TypeError` for malformed subprocess requests.
Auto-flush also currently accepts any string `channel` via `cast("Any", channel)`, while knowledge refresh restricts channel to `"matrix"` or `"openai_compat"`.
A shared helper would need policy flags for strict-vs-lenient validation and channel enforcement.

## Proposed Generalization

1. Replace `_coerce_agent_session()` and the local `storage.get_session(..., SessionType.AGENT)` call with `get_agent_session()` from `mindroom.agent_storage`.
2. Add a focused execution-identity payload helper near `mindroom.tool_system.worker_routing`, for example `tool_execution_identity_to_payload()` and `tool_execution_identity_from_payload(..., strict: bool)`.
3. Migrate auto-flush to lenient identity hydration that returns `None` on malformed persisted state.
4. Migrate knowledge refresh to strict identity hydration that raises `TypeError` with its current error semantics.
5. Leave auto-flush JSON state persistence local unless more state files need the same corruption-recovery and sanitization rules.

## Risk/tests

Session-loading dedupe is low risk if covered by existing auto-flush tests that persist Agno sessions as both `AgentSession` objects and dict payloads.
Identity-helper extraction is moderate risk because the two call sites intentionally differ on malformed payload behavior.
Tests should cover optional fields, invalid non-string optional values, invalid channel handling, and malformed persisted auto-flush entries being sanitized rather than crashing.
