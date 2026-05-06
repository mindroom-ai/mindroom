## Summary

Top duplication candidates:

1. `src/mindroom/matrix/client_thread_history.py:76` defines `_thread_history_result`, a local wrapper that duplicates `thread_history_result` without changing behavior.
2. `src/mindroom/matrix/conversation_cache.py:436` and `src/mindroom/matrix/cache/thread_reads.py:79` both manually express detached/full-history copies of `ThreadHistoryResult` that overlap with `thread_history_result`'s coercion behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ThreadHistoryResult	class	lines 26-57	related-only	ThreadHistoryResult Sequence wrapper is_full_history diagnostics __iter__ __len__ __getitem__ __eq__	src/mindroom/matrix/cache/thread_reads.py:79; src/mindroom/matrix/conversation_cache.py:436; src/mindroom/approval_manager.py:91
ThreadHistoryResult.__iter__	method	lines 33-35	none-found	return iter(self.messages) __iter__ Sequence wrapper	src/mindroom/approval_manager.py:91
ThreadHistoryResult.__len__	method	lines 37-39	none-found	return len(self.messages) __len__ Sequence wrapper	src/mindroom/approval_manager.py:91; src/mindroom/turn_store.py:52; src/mindroom/handled_turns.py:142
ThreadHistoryResult.__getitem__	method	lines 42-42; lines 45-45; lines 47-49	not-a-behavior-symbol; none-found	overload __getitem__ index int; overload __getitem__ index slice; return self.messages[index] __getitem__ Sequence wrapper	none
ThreadHistoryResult.__eq__	method	lines 51-57	none-found	__eq__ ThreadHistoryResult Sequence list-style comparison	src/mindroom/matrix/cache/thread_history_result.py:51 only
thread_history_result	function	lines 60-80	duplicate-found	thread_history_result ThreadHistoryResult diagnostics is_full_history list(history) copy	src/mindroom/matrix/client_thread_history.py:76; src/mindroom/matrix/conversation_cache.py:436; src/mindroom/matrix/cache/thread_reads.py:79
```

## Findings

### 1. Local `_thread_history_result` duplicates the shared constructor helper

- Primary behavior: `src/mindroom/matrix/cache/thread_history_result.py:60` copies a message sequence into `ThreadHistoryResult`, copies diagnostics, and preserves existing diagnostics when wrapping an existing `ThreadHistoryResult` with no override.
- Duplicate behavior: `src/mindroom/matrix/client_thread_history.py:76` defines `_thread_history_result` with the same public purpose and simply calls `thread_history_result`.
- Why duplicated: the local helper has the same name shape, same metadata purpose, and no additional behavior.
- Differences to preserve: none currently visible; all call sites in `client_thread_history.py` can call the shared helper directly.

### 2. Detached result-copy logic is repeated around turn memoization and full-history coercion

- Primary behavior: `src/mindroom/matrix/cache/thread_history_result.py:60` creates a detached `ThreadHistoryResult` by copying `history` to a list and copying diagnostics.
- Related duplicate behavior: `src/mindroom/matrix/conversation_cache.py:436` implements `_copy_thread_read_result` by calling `thread_history_result(list(result), is_full_history=result.is_full_history, diagnostics=result.diagnostics)`.
- Related duplicate behavior: `src/mindroom/matrix/cache/thread_reads.py:79` implements `_full_history_result`, including a special branch for `ThreadHistoryResult` that preserves diagnostics while forcing `is_full_history=True`.
- Why related: these are not independent constructors, but they repeat policy decisions already centralized in `thread_history_result`: detach message storage, preserve diagnostics, and set hydration metadata.
- Differences to preserve: `_copy_thread_read_result` preserves the original `is_full_history`; `_full_history_result` intentionally forces `is_full_history=True`.

## Proposed Generalization

1. Remove `src/mindroom/matrix/client_thread_history.py:_thread_history_result` and call `thread_history_result` directly at its three call sites.
2. Consider replacing `MatrixConversationCache._copy_thread_read_result` with `thread_history_result(result, is_full_history=result.is_full_history)` so the existing helper owns diagnostics preservation for existing results.
3. Consider simplifying `ThreadReadPolicy._full_history_result` to `thread_history_result(history, is_full_history=True)`, relying on the existing helper to preserve diagnostics when `history` is already a `ThreadHistoryResult`.

No broader refactor is recommended.

## Risk/tests

- Risk is low for removing the local wrapper because it only delegates.
- The memoization/full-history simplifications should preserve detached-list behavior and diagnostics copying; tests should cover cached thread reads, dispatch-safe snapshots, and turn-scope memoization.
- Relevant test areas: `tests/test_thread_history.py`, `tests/test_threading_error.py`, and thread-read assertions in `tests/test_multi_agent_bot.py`.
