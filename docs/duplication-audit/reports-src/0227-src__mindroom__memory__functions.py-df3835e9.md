# Duplication Audit: `src/mindroom/memory/functions.py`

## Summary

Top duplication candidate: the public memory facade repeats the same disabled/file/mem0 backend dispatch shape across add, search, list, get, update, and delete operations.
This repetition is active within `src/mindroom/memory/functions.py`, but I did not find another independent implementation of the same facade elsewhere under `./src`.
The closest related behavior outside the file is backend-specific CRUD in `_file_backend.py` and `_mem0_backend.py`, plus tool/UI call sites that consume the facade rather than duplicating it.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MemoryPromptParts	class	lines 61-65	related-only	MemoryPromptParts prompt parts session_preamble turn_context	 src/mindroom/ai.py:120; src/mindroom/ai.py:737; src/mindroom/memory/__init__.py:11
_create_memory_factory	function	lines 68-71	none-found	create_memory_instance partial runtime_paths create_memory	 src/mindroom/memory/config.py:152; src/mindroom/memory/_mem0_backend.py:63; src/mindroom/memory/_mem0_backend.py:272
_search_file_backend_memories	function	lines 75-94	related-only	search_file_agent_memories timed memory_search file_backend	 src/mindroom/memory/_file_backend.py:623; src/mindroom/memory/functions.py:216
_search_mem0_backend_memories	async_function	lines 98-118	related-only	search_mem0_agent_memories create_memory timed memory_search mem0_backend	 src/mindroom/memory/_mem0_backend.py:303; src/mindroom/memory/functions.py:226
_load_agent_file_entrypoint_context	function	lines 122-142	related-only	load_scope_entrypoint_context resolve_file_memory_resolution agent_scope_user_id File memory entrypoint	 src/mindroom/memory/_file_backend.py:19; src/mindroom/memory/_policy.py:202; src/mindroom/memory/functions.py:401
add_agent_memory	async_function	lines 145-176	duplicate-found	use_disabled_memory_backend use_file_memory_backend add_file_agent_memory add_mem0_agent_memory	 src/mindroom/memory/functions.py:202; src/mindroom/memory/functions.py:238; src/mindroom/memory/functions.py:272; src/mindroom/memory/functions.py:303; src/mindroom/memory/functions.py:338; src/mindroom/custom_tools/memory.py:77
append_agent_daily_memory	function	lines 179-198	related-only	append_agent_daily_file_memory append_agent_daily_memory auto_flush daily memory	 src/mindroom/memory/_file_backend.py:553; src/mindroom/memory/auto_flush.py:849
search_agent_memories	async_function	lines 202-235	duplicate-found	use_disabled_memory_backend use_file_memory_backend search_file_agent_memories search_mem0_agent_memories	 src/mindroom/memory/functions.py:145; src/mindroom/memory/functions.py:238; src/mindroom/memory/_file_backend.py:623; src/mindroom/memory/_mem0_backend.py:303; src/mindroom/custom_tools/memory.py:106
list_all_agent_memories	async_function	lines 238-269	duplicate-found	use_disabled_memory_backend use_file_memory_backend list_file_agent_memories list_mem0_agent_memories	 src/mindroom/memory/functions.py:145; src/mindroom/memory/functions.py:202; src/mindroom/memory/_file_backend.py:684; src/mindroom/memory/_mem0_backend.py:344; src/mindroom/memory/auto_flush.py:465
get_agent_memory	async_function	lines 272-300	duplicate-found	caller_uses_disabled_memory_backend caller_uses_file_memory_backend get_file_agent_memory get_mem0_agent_memory	 src/mindroom/memory/functions.py:303; src/mindroom/memory/functions.py:338; src/mindroom/memory/_file_backend.py:707; src/mindroom/memory/_mem0_backend.py:366; src/mindroom/custom_tools/memory.py:173
update_agent_memory	async_function	lines 303-335	duplicate-found	caller_uses_disabled_memory_backend caller_uses_file_memory_backend update_file_agent_memory update_mem0_agent_memory	 src/mindroom/memory/functions.py:272; src/mindroom/memory/functions.py:338; src/mindroom/memory/_file_backend.py:738; src/mindroom/memory/_mem0_backend.py:392; src/mindroom/custom_tools/memory.py:202
delete_agent_memory	async_function	lines 338-367	duplicate-found	caller_uses_disabled_memory_backend caller_uses_file_memory_backend delete_file_agent_memory delete_mem0_agent_memory	 src/mindroom/memory/functions.py:272; src/mindroom/memory/functions.py:303; src/mindroom/memory/_file_backend.py:780; src/mindroom/memory/_mem0_backend.py:435; src/mindroom/custom_tools/memory.py:236
build_memory_prompt_parts	async_function	lines 371-417	related-only	build_memory_prompt_parts _format_memories_as_context File memory entrypoint session_preamble turn_context	 src/mindroom/memory/_prompting.py:18; src/mindroom/ai.py:737; src/mindroom/ai.py:116
build_memory_enhanced_prompt	async_function	lines 420-440	related-only	build_memory_enhanced_prompt prompt_chunks session_preamble turn_context join	 src/mindroom/ai.py:116; src/mindroom/memory/functions.py:430
store_conversation_memory	async_function	lines 443-493	duplicate-found	store_conversation_memory team_uses_disabled_memory_backend team_uses_file_memory_backend build_memory_messages store_file_conversation_memory store_mem0_conversation_memory	 src/mindroom/response_runner.py:2184; src/mindroom/bot.py:1921; src/mindroom/memory/functions.py:145; src/mindroom/memory/functions.py:202; src/mindroom/memory/_prompting.py:52
```

## Findings

1. Repeated public backend dispatch in memory CRUD methods.
`add_agent_memory`, `search_agent_memories`, and `list_all_agent_memories` each perform the same three-way decision: return the disabled-backend empty result, call the file backend synchronously, otherwise call the mem0 backend with `_create_memory_factory(runtime_paths)`.
The same shape appears for caller-context operations in `get_agent_memory`, `update_agent_memory`, and `delete_agent_memory`, using `caller_uses_disabled_memory_backend` and `caller_uses_file_memory_backend`.
The repeated behavior is local to `src/mindroom/memory/functions.py:145`, `src/mindroom/memory/functions.py:202`, `src/mindroom/memory/functions.py:238`, `src/mindroom/memory/functions.py:272`, `src/mindroom/memory/functions.py:303`, and `src/mindroom/memory/functions.py:338`.
The differences to preserve are return values on disabled memory (`None`, `[]`, or no-op), sync file backend calls versus async mem0 calls, operation-specific parameters, and the caller-context versus single-agent policy helpers.

2. Store-conversation backend dispatch repeats the same policy idea with team support.
`store_conversation_memory` in `src/mindroom/memory/functions.py:443` repeats disabled/file/mem0 selection, but it must handle `agent_name: str | list[str]`.
It is related to the CRUD facade but not identical because teams use `team_uses_disabled_memory_backend` and `team_uses_file_memory_backend`, and mem0 persistence first converts thread history through `build_memory_messages`.
Call sites in `src/mindroom/response_runner.py:2184` and `src/mindroom/bot.py:1921` delegate to this function rather than duplicating the storage behavior.

3. Prompt assembly is related to AI prompt composition but not a duplicate.
`build_memory_prompt_parts` builds a stable file-memory preamble and a turn-local searched-memory block at `src/mindroom/memory/functions.py:371`.
`_compose_current_turn_prompt` in `src/mindroom/ai.py:116` joins raw prompt, memory turn context, and model prompt, but it operates after memory prompt parts are already built.
The shared operation is chunk joining with blank-line separators; the behavior is intentionally split between memory retrieval and AI turn construction.

## Proposed Generalization

A minimal future refactor could add one private helper in `src/mindroom/memory/functions.py` for facade backend selection, likely returning a small enum or dataclass for disabled/file/mem0 after evaluating either an agent name or caller context.
That would remove repeated policy checks while keeping operation-specific calls explicit.
No cross-module helper is recommended because the duplication is contained inside the facade and the backend modules already own backend-specific behavior.

## Risk/tests

Refactoring the dispatch would risk changing disabled-backend return semantics and accidentally awaiting or not awaiting the sync file backend path.
Tests should cover each public facade function for `none`, `file`, and `mem0` backends, including caller-context/team cases and `store_conversation_memory` with and without `thread_history`.
Prompt tests should keep `session_preamble`, `turn_context`, and legacy `build_memory_enhanced_prompt` ordering stable.
