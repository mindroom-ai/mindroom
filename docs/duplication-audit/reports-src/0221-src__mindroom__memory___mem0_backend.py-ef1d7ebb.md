## Summary

The strongest duplication candidate is the backend-level memory CRUD lifecycle shared by `src/mindroom/memory/_mem0_backend.py` and `src/mindroom/memory/_file_backend.py`.
Both backends resolve visible memory scopes, locate an anchor memory, derive replica targets, mutate update/delete targets, and expose parallel public add/search/list/get/update/delete/store functions.
The duplication is functional rather than literal because each backend has distinct storage primitives, so any refactor should be small and limited to backend-neutral orchestration helpers.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_mem0_results	function	lines 33-39	none-found	_mem0_results, get_all results payload, search results payload	src/mindroom/memory/_shared.py:43, src/mindroom/memory/config.py:151, src/mindroom/tools/mem0.py:103
_scope_filter	function	lines 42-43	none-found	filters user_id, scope filter, get_all filters, memory.search filters	src/mindroom/memory/_mem0_backend.py:80, src/mindroom/memory/_mem0_backend.py:96, src/mindroom/memory/_mem0_backend.py:109, src/mindroom/memory/_mem0_backend.py:150, src/mindroom/memory/_mem0_backend.py:363
_primary_mem0_storage_path	function	lines 46-60	related-only	effective_storage_paths_for_context first path, resolve primary storage, agent state root	src/mindroom/memory/_policy.py:59, src/mindroom/memory/_file_backend.py:635, src/mindroom/memory/_file_backend.py:695
_create_mem0_memory_instance	async_function	lines 64-70	related-only	create_memory instance timing wrapper, create_memory_instance, AsyncMemory from_config	src/mindroom/memory/functions.py:68, src/mindroom/memory/config.py:151
_search_mem0_agent_scope	async_function	lines 74-86	duplicate-found	search agent scope memories, agent_scope_user_id, backend agent search	src/mindroom/memory/_file_backend.py:585
_search_mem0_team_scope	async_function	lines 90-96	duplicate-found	search team scope memories, team scope search	src/mindroom/memory/_file_backend.py:604
_get_scoped_memory_by_id	async_function	lines 99-130	duplicate-found	get memory by id visible to caller, allowed memory user ids, scoped get	src/mindroom/memory/_file_backend.py:354, src/mindroom/memory/_file_backend.py:707
_mem0_replica_key	function	lines 133-138	none-found	MEM0_REPLICA_KEY, mindroom_replica_key, metadata replica key	src/mindroom/memory/_shared.py:95, src/mindroom/memory/functions.py:490
_find_mem0_replica_memory_ids	async_function	lines 141-172	duplicate-found	find replica memory ids, anchor_result memory metadata, replica ids	src/mindroom/memory/_file_backend.py:424
_find_mem0_anchor_memory_result	async_function	lines 175-196	duplicate-found	find anchor memory result, allowed scopes storage paths get by id	src/mindroom/memory/_file_backend.py:451
_mem0_mutation_target_ids	async_function	lines 199-214	duplicate-found	mutation target ids, direct match else replica ids	src/mindroom/memory/_file_backend.py:481
_mutate_mem0_memory_targets	async_function	lines 217-254	duplicate-found	mutate memory targets, storage_paths_for_scope_user_id, update delete targets	src/mindroom/memory/_file_backend.py:498
_add_mem0_scope_messages	async_function	lines 257-269	related-only	memory.add failure logging, add scope messages, Failed to add memory	src/mindroom/memory/_mem0_backend.py:295, src/mindroom/memory/_mem0_backend.py:522, src/mindroom/custom_tools/memory.py:76
add_mem0_agent_memory	async_function	lines 272-300	duplicate-found	add agent memory backend, explicit add memory, append file agent memory	src/mindroom/memory/_file_backend.py:533, src/mindroom/memory/functions.py:145
search_mem0_agent_memories	async_function	lines 303-341	duplicate-found	search agent memories including team memories, dedupe memory text, return limit	src/mindroom/memory/_file_backend.py:623, src/mindroom/memory/functions.py:201
list_mem0_agent_memories	async_function	lines 344-363	duplicate-found	list agent memories, scope user id, limit	src/mindroom/memory/_file_backend.py:684, src/mindroom/memory/functions.py:238
get_mem0_agent_memory	async_function	lines 366-389	duplicate-found	get visible memory allowed scopes storage paths	src/mindroom/memory/_file_backend.py:707, src/mindroom/memory/functions.py:272
update_mem0_agent_memory	async_function	lines 392-432	duplicate-found	update memory anchor result mutate targets not found	src/mindroom/memory/_file_backend.py:738, src/mindroom/memory/functions.py:303
delete_mem0_agent_memory	async_function	lines 435-474	duplicate-found	delete memory anchor result mutate targets not found	src/mindroom/memory/_file_backend.py:780, src/mindroom/memory/functions.py:338
store_mem0_conversation_memory	async_function	lines 477-541	duplicate-found	store conversation memory scopes metadata target storage paths team/agent log	src/mindroom/memory/_file_backend.py:821, src/mindroom/memory/functions.py:443
```

## Findings

1. `search_mem0_agent_memories` in `src/mindroom/memory/_mem0_backend.py:303` duplicates the search orchestration in `search_file_agent_memories` at `src/mindroom/memory/_file_backend.py:623`.
Both search the agent scope first, collect memory text to deduplicate results, then search all team scopes returned by `get_team_ids_for_agent`, append non-duplicate memories, and cap the result list to `limit`.
Differences to preserve: file search resolves each team storage path separately and sorts by score at the end, while mem0 uses one primary memory instance and logs team/total counts.

2. The get/update/delete lifecycle is duplicated across backend-specific helpers.
`_find_mem0_anchor_memory_result` at `src/mindroom/memory/_mem0_backend.py:175`, `_mem0_mutation_target_ids` at `src/mindroom/memory/_mem0_backend.py:199`, `_mutate_mem0_memory_targets` at `src/mindroom/memory/_mem0_backend.py:217`, `update_mem0_agent_memory` at `src/mindroom/memory/_mem0_backend.py:392`, and `delete_mem0_agent_memory` at `src/mindroom/memory/_mem0_backend.py:435` mirror the file backend's `_find_file_anchor_memory_result` at `src/mindroom/memory/_file_backend.py:451`, `_file_mutation_target_ids` at `src/mindroom/memory/_file_backend.py:481`, `_mutate_file_memory_targets` at `src/mindroom/memory/_file_backend.py:498`, `update_file_agent_memory` at `src/mindroom/memory/_file_backend.py:738`, and `delete_file_agent_memory` at `src/mindroom/memory/_file_backend.py:780`.
Both implementations resolve allowed scope IDs, map scopes to storage paths, find an anchor result, derive direct or replica target IDs, perform update/delete, log success, and raise `MemoryNotFoundError` if no target is mutated.
Differences to preserve: mem0 needs async CRUD and replica-key matching through metadata, while file memory mutates markdown entries and can only safely infer replicas from a unique memory/source-file match.

3. Agent/team conversation persistence is duplicated between `store_mem0_conversation_memory` at `src/mindroom/memory/_mem0_backend.py:477` and `store_file_conversation_memory` at `src/mindroom/memory/_file_backend.py:821`.
Both derive target storage paths via `effective_storage_paths_for_context`, choose `agent_scope_user_id` versus `build_team_user_id`, write to every target storage path, and emit separate agent/team log messages.
Differences to preserve: mem0 stores structured message lists with session metadata and a `MEM0_REPLICA_KEY` for team replicas, while file memory stores a condensed prompt string and passes a shared generated memory ID for team writes.

4. The public backend operations `add_mem0_agent_memory`, `list_mem0_agent_memories`, and `get_mem0_agent_memory` have direct file-backend equivalents at `src/mindroom/memory/_file_backend.py:533`, `src/mindroom/memory/_file_backend.py:684`, and `src/mindroom/memory/_file_backend.py:707`.
The shared behavior is scope-aware storage resolution plus backend-specific add/list/get calls.
This duplication is lower-impact because the bodies are short and the backend-specific operations are the majority of each function.

## Proposed Generalization

A minimal refactor, if desired later, would be to add a small backend-neutral helper module under `src/mindroom/memory/`, for example `_backend_flow.py`.
It should not abstract mem0 or file storage APIs directly.
Instead, it could hold pure or callback-based orchestration helpers for:

1. Deduplicating and limiting agent-plus-team search results by memory text.
2. Iterating allowed scope IDs and their storage paths for anchor lookup.
3. Shared update/delete not-found handling after a backend-specific mutation function returns a target count.
4. Building common conversation write metadata such as target storage paths and scope user IDs, while leaving payload construction to each backend.

No refactor is recommended for `_mem0_results`, `_scope_filter`, `_mem0_replica_key`, or `_create_mem0_memory_instance`; they are tiny mem0-specific adapters and abstraction would not reduce meaningful complexity.

## Risk/tests

The main risk in deduplicating this code is weakening backend-specific behavior that currently differs for good reasons.
Mem0 replica matching relies on metadata keys and asynchronous CRUD, while file memory relies on markdown parsing, path IDs, and uniqueness checks.
Search result ordering is also different: file memory sorts by score after merging team results, while mem0 preserves agent results first and appends unique team results before truncating.

Tests that would need attention before any implementation include backend-parity tests for search deduplication, allowed-scope get/update/delete behavior, team-memory replica updates/deletes across multiple storage paths, and conversation memory writes for both agent and team contexts.
This audit made no production-code changes.
