## Summary

No meaningful duplication found in the shared memory contracts.
`MemoryResult`, `ScopedMemoryWriter`, `ScopedMemoryCrud`, `MemoryNotFoundError`, and `FileMemoryResolution` are already centralized and imported by the file and mem0 backends.
The only related repeated behavior is local UUID-based identifier generation in non-memory runtime modules, but those IDs have different formats and semantics from memory IDs.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MemoryResult	class	lines 17-27	none-found	MemoryResult TypedDict results metadata user_id	src/mindroom/memory/_file_backend.py:130; src/mindroom/memory/_file_backend.py:153; src/mindroom/memory/_mem0_backend.py:33; src/mindroom/memory/_prompting.py:18
ScopedMemoryWriter	class	lines 30-40	none-found	ScopedMemoryWriter Protocol add messages user_id metadata	src/mindroom/memory/_mem0_backend.py:19; src/mindroom/memory/_mem0_backend.py:259; src/mindroom/memory/config.py:152
ScopedMemoryWriter.add	async_method	lines 33-40	none-found	async add messages user_id metadata memory protocol	src/mindroom/memory/_mem0_backend.py:259; src/mindroom/memory/_mem0_backend.py:272; src/mindroom/memory/config.py:152
ScopedMemoryCrud	class	lines 43-70	none-found	ScopedMemoryCrud get get_all update delete search protocol	src/mindroom/memory/_mem0_backend.py:28; src/mindroom/memory/_mem0_backend.py:64; src/mindroom/memory/functions.py:68
ScopedMemoryCrud.get	async_method	lines 46-47	none-found	memory.get memory_id scoped get protocol	src/mindroom/memory/_mem0_backend.py:99; src/mindroom/memory/_mem0_backend.py:105; src/mindroom/memory/functions.py:272
ScopedMemoryCrud.get_all	async_method	lines 49-55	none-found	get_all filters top_k results scoped memory	src/mindroom/memory/_mem0_backend.py:109; src/mindroom/memory/_mem0_backend.py:150; src/mindroom/memory/_mem0_backend.py:314
ScopedMemoryCrud.update	async_method	lines 57-58	none-found	memory.update memory_id data scoped protocol	src/mindroom/memory/_mem0_backend.py:217; src/mindroom/memory/_mem0_backend.py:392; src/mindroom/memory/functions.py:303
ScopedMemoryCrud.delete	async_method	lines 60-61	none-found	memory.delete memory_id scoped protocol	src/mindroom/memory/_mem0_backend.py:217; src/mindroom/memory/_mem0_backend.py:435; src/mindroom/memory/functions.py:338
ScopedMemoryCrud.search	async_method	lines 63-70	none-found	memory.search query filters top_k results	src/mindroom/memory/_mem0_backend.py:73; src/mindroom/memory/_mem0_backend.py:89; src/mindroom/memory/functions.py:97
MemoryNotFoundError	class	lines 73-77	none-found	MemoryNotFoundError No memory found id	src/mindroom/memory/_file_backend.py:758; src/mindroom/memory/_file_backend.py:777; src/mindroom/memory/_mem0_backend.py:415; src/mindroom/memory/_mem0_backend.py:474
MemoryNotFoundError.__init__	method	lines 76-77	none-found	No memory found with id error message	src/mindroom/custom_tools/memory.py:182; src/mindroom/memory/_file_backend.py:758; src/mindroom/memory/_mem0_backend.py:415
FileMemoryResolution	class	lines 81-87	none-found	FileMemoryResolution storage_path runtime_paths use_configured_path agent_memory_scope_path	src/mindroom/memory/_policy.py:172; src/mindroom/memory/_policy.py:202; src/mindroom/memory/_file_backend.py:46
new_memory_id	function	lines 98-101	related-only	uuid4 timestamp id generation m_ uuid hex	src/mindroom/memory/_file_backend.py:217; src/mindroom/memory/_file_backend.py:842; src/mindroom/memory/functions.py:490; src/mindroom/teams.py:191; src/mindroom/bot.py:769; src/mindroom/history/compaction.py:242; src/mindroom/response_runner.py:466
```

## Findings

No real duplication found.

`new_memory_id` in `src/mindroom/memory/_shared.py:98` is related to UUID-based ID creation in `src/mindroom/teams.py:191`, `src/mindroom/bot.py:769`, `src/mindroom/history/compaction.py:242`, and `src/mindroom/response_runner.py:466`.
Those call sites create retry run IDs, hook correlation IDs, and replay run IDs, while `new_memory_id` creates timestamped memory IDs with an `m_` prefix.
The behavior is not the same enough to share without introducing a vague generic ID helper.

## Proposed Generalization

No refactor recommended.

## Risk/Tests

Changing these shared contracts would affect both file-backed and mem0-backed memory paths.
If a future refactor touches this module, tests should cover memory add/list/search/get/update/delete for both backends and any tool-facing formatting that depends on `[id=...]` values.
