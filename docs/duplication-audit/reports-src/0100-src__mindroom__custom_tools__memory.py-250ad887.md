## Summary

Top duplication candidates in `src/mindroom/custom_tools/memory.py`:

1. `search_memories` and `list_memories` duplicate the same model-facing memory-result formatting loop.
2. `add_memory`, `update_memory`, and `delete_memory` duplicate the same mutating-tool wrapper pattern: call the memory facade with shared identity/context fields, log on exception, return a failure string, otherwise return a confirmation string.
3. `get_memory`, `search_memories`, and `list_memories` all format `MemoryResult` IDs and text directly, while the file backend has a related private row formatter for persisted markdown entries.

No broad cross-module duplication of `MemoryTools` itself was found.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MemoryTools	class	lines 34-253	related-only	MemoryTools class Toolkit memory tools agent_name execution_identity	src/mindroom/agents.py:480; src/mindroom/tools/mem0.py:13; src/mindroom/custom_tools/dynamic_tools.py:21; src/mindroom/custom_tools/compact_context.py:20; src/mindroom/custom_tools/delegate.py:34
MemoryTools.__init__	method	lines 37-61	related-only	super().__init__ name memory tools add_memory search_memories list_memories execution_identity	src/mindroom/agents.py:480; src/mindroom/custom_tools/dynamic_tools.py:21; src/mindroom/custom_tools/compact_context.py:20; src/mindroom/custom_tools/delegate.py:34
MemoryTools.add_memory	async_method	lines 63-90	duplicate-found	add_agent_memory Failed to add memory via tool Memorized explicit_tool	src/mindroom/memory/functions.py:145; src/mindroom/custom_tools/memory.py:188; src/mindroom/custom_tools/memory.py:222
MemoryTools.search_memories	async_method	lines 92-125	duplicate-found	search_agent_memories No relevant memories Found memory id enumerate results	src/mindroom/memory/functions.py:202; src/mindroom/custom_tools/memory.py:127; src/mindroom/memory/_file_backend.py:188
MemoryTools.list_memories	async_method	lines 127-158	duplicate-found	list_all_agent_memories No memories stored All memories id enumerate results	src/mindroom/memory/functions.py:238; src/mindroom/custom_tools/memory.py:92; src/mindroom/memory/_file_backend.py:188
MemoryTools.get_memory	async_method	lines 160-186	related-only	get_agent_memory No memory found id result.get memory Failed to get memory	src/mindroom/memory/functions.py:272; src/mindroom/memory/_shared.py:73; src/mindroom/memory/_file_backend.py:758; src/mindroom/memory/_mem0_backend.py:415
MemoryTools.update_memory	async_method	lines 188-220	duplicate-found	update_agent_memory Failed to update memory via tool Updated memory id	src/mindroom/memory/functions.py:303; src/mindroom/custom_tools/memory.py:63; src/mindroom/custom_tools/memory.py:222; src/mindroom/memory/_shared.py:73
MemoryTools.delete_memory	async_method	lines 222-253	duplicate-found	delete_agent_memory Failed to delete memory via tool Deleted memory id	src/mindroom/memory/functions.py:338; src/mindroom/custom_tools/memory.py:63; src/mindroom/custom_tools/memory.py:188; src/mindroom/memory/_shared.py:73
```

## Findings

### 1. Duplicate model-facing memory list formatting inside `MemoryTools`

`MemoryTools.search_memories` formats non-empty results at `src/mindroom/custom_tools/memory.py:118` through `src/mindroom/custom_tools/memory.py:122`.
`MemoryTools.list_memories` repeats the same loop at `src/mindroom/custom_tools/memory.py:151` through `src/mindroom/custom_tools/memory.py:155`.
Both build a header with the result count, enumerate results from one, extract `mem.get("id", "?")`, extract `mem.get("memory", "")`, and join lines with newlines.

Differences to preserve:

- Empty-result text differs: `"No relevant memories found."` for search and `"No memories stored yet."` for list.
- Header text differs: `"Found {len(results)} memory(ies):"` versus `"All memories ({len(results)}):"`.
- Default limits differ: `5` for search and `50` for list.

This is active duplicated behavior in the primary file.

### 2. Repeated mutating-tool wrapper flow in add/update/delete

`MemoryTools.add_memory` wraps `add_agent_memory` at `src/mindroom/custom_tools/memory.py:76` through `src/mindroom/custom_tools/memory.py:90`.
`MemoryTools.update_memory` wraps `update_agent_memory` at `src/mindroom/custom_tools/memory.py:201` through `src/mindroom/custom_tools/memory.py:220`.
`MemoryTools.delete_memory` wraps `delete_agent_memory` at `src/mindroom/custom_tools/memory.py:235` through `src/mindroom/custom_tools/memory.py:253`.
Each method calls a memory facade with `self._agent_name`, `self._storage_path`, `self._config`, `self._runtime_paths`, and `self._execution_identity`, catches `Exception`, logs with `logger.exception`, returns a `Failed to ...` string, and otherwise returns a success confirmation.

Differences to preserve:

- `add_memory` passes `metadata={"source": "explicit_tool"}` and returns `"Memorized: {content}"`.
- `update_memory` passes both ID and new content and includes `memory_id` in structured logging.
- `delete_memory` passes only ID and includes `memory_id` in structured logging.
- Failure verbs differ: `"store memory"`, `"update memory"`, and `"delete memory"`.

This duplication is local to `MemoryTools`.
The underlying facade functions in `src/mindroom/memory/functions.py:145`, `src/mindroom/memory/functions.py:303`, and `src/mindroom/memory/functions.py:338` are related but not duplicate tool behavior because they route between file, mem0, and disabled backends rather than formatting model-facing tool responses.

### 3. Related, but not directly duplicate, memory row formatting in the file backend

`MemoryTools` emits model-facing rows as `"1. [id={mid}] {memory}"` at `src/mindroom/custom_tools/memory.py:121` and `src/mindroom/custom_tools/memory.py:154`.
`MemoryTools.get_memory` emits a single row as `"[id={id}] {memory}"` at `src/mindroom/custom_tools/memory.py:183`.
The file backend has `_format_entry_line` at `src/mindroom/memory/_file_backend.py:188`, which emits persisted markdown rows as `"- [id={memory_id}] {normalized_content}"`.

These are related because all encode the same ID-plus-memory display convention, but they serve different surfaces:

- `MemoryTools` returns model-facing numbered or single-line text.
- `_format_entry_line` writes canonical file-memory markdown.
- The backend normalizes whitespace before persistence, while `MemoryTools` displays whatever the backend returns.

No refactor is recommended across these surfaces unless a future change needs one shared display format.

## Proposed Generalization

1. Add a small private helper in `src/mindroom/custom_tools/memory.py`, for example `_format_memory_results(results, *, header, empty_message) -> str`.
2. Use it from `search_memories` and `list_memories`, preserving each method's empty message and header.
3. Optionally add a private `_format_memory_result(memory, *, index=None, fallback_id="?") -> str` if `get_memory` should share ID/text extraction with list/search.
4. Consider a private async wrapper only if another mutating memory tool is added; with three call sites, a generic wrapper may obscure more than it removes because the argument lists and success messages differ.

No broad module extraction is recommended.

## Risk/tests

Primary risk is model-facing string drift.
Tests should assert exact outputs for:

- Empty search result.
- Empty list result.
- Non-empty search and list results, including missing `id` fallback to `"?"`.
- `get_memory` returning `None`.
- Exception paths for add/search/list/get/update/delete, if the current behavior is intentionally retained.

No production code was edited.
