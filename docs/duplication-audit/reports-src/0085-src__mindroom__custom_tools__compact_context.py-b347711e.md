## Summary

No meaningful duplication found.
`src/mindroom/custom_tools/compact_context.py` is a thin Agno toolkit adapter.
The behavior that schedules manual compaction is centralized in `src/mindroom/history/manual.py`, and related context/session helpers are already shared through `src/mindroom/tool_system/runtime_context.py`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
CompactContextTools	class	lines 20-63	related-only	CompactContextTools compact_context Toolkit tools request_compaction_before_next_reply	src/mindroom/tools/compact_context.py:17; src/mindroom/agents.py:541; src/mindroom/custom_tools/thread_summary.py:19; src/mindroom/custom_tools/thread_tags.py:64; src/mindroom/custom_tools/memory.py:34
CompactContextTools.__init__	method	lines 23-34	related-only	super().__init__ name tools self.compact_context Toolkit direct agent toolkit	src/mindroom/custom_tools/thread_summary.py:22; src/mindroom/custom_tools/thread_tags.py:67; src/mindroom/custom_tools/memory.py:37; src/mindroom/agents.py:541
CompactContextTools.compact_context	async_method	lines 36-63	related-only	request_compaction_before_next_reply resolve_current_session_id get_tool_runtime_context run_context.session_state force_compact_before_next_run	src/mindroom/history/manual.py:34; src/mindroom/tool_system/runtime_context.py:395; src/mindroom/tool_system/runtime_context.py:416; src/mindroom/history/storage.py:93; src/mindroom/history/runtime.py:1403; src/mindroom/history/compaction.py:227
```

## Findings

No real duplication was found for manual context compaction.

`CompactContextTools.compact_context` delegates the stateful compaction request to `request_compaction_before_next_reply` in `src/mindroom/history/manual.py:34`.
That helper owns the behavioral core: opening the history scope, validating compaction availability, setting `force_compact_before_next_run`, persisting the session, and optionally recording pending scope keys in Agno `session_state`.
The custom tool only performs tool-call boundary work: requiring an active Agno agent, resolving the current session ID with `resolve_current_session_id`, passing runtime fields, and writing returned session state back to `run_context`.

The nearest related behavior is not duplicated.
`src/mindroom/history/runtime.py:1403` consumes pending force-compaction state before a run, `src/mindroom/history/storage.py:93` records pending force-compaction scopes in session state, and `src/mindroom/history/compaction.py:227` reads tool runtime context to emit hooks.
Those are separate phases of the same compaction lifecycle, not repeated implementations of the tool request path.

The toolkit wrapper shape is common but benign.
`ThreadSummaryTools`, `ThreadTagsTools`, and `MemoryTools` also subclass `Toolkit`, store dependencies, and call `super().__init__(name=..., tools=[...])`.
This is framework-required registration boilerplate rather than duplicated domain behavior.

## Proposed Generalization

No refactor recommended.
The primary behavior is already centralized in `src/mindroom/history/manual.py`, and extracting a generic toolkit constructor helper would obscure simple Agno registration without removing meaningful behavior duplication.

## Risk/tests

No production change is recommended.
If this tool is changed later, tests should cover:

- `compact_context` returns an error when `agent` is missing.
- session ID resolution prefers explicit execution identity and falls back to tool runtime context.
- successful manual compaction updates `run_context.session_state` when present.
- unavailable compaction returns the user-facing budget/configuration error from `history.manual`.

Assumption: metadata-only registration in `src/mindroom/tools/compact_context.py:17` is intentionally separate from toolkit instantiation in `src/mindroom/agents.py:541`.
