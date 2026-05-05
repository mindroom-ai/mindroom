## Summary

No meaningful duplication found.
The primary module is a small facade over `mindroom.knowledge.registry`; most behavior is composition of existing registry helpers for API callers rather than duplicated business logic.
The only exact overlap is `mark_knowledge_source_changed_async`, which intentionally re-exports the registry async stale-marker without exposing registry internals.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
KnowledgeIndexStatus	class	lines 21-36	related-only	KnowledgeIndexStatus indexed_count refresh_state availability persisted_index_status last_error published_revision metadata_exists	src/mindroom/knowledge/registry.py:69; src/mindroom/knowledge/registry.py:97; src/mindroom/api/knowledge.py:233; src/mindroom/api/knowledge.py:515; src/mindroom/api/knowledge.py:657
KnowledgeIndexStatus.initial_sync_complete	method	lines 34-36	related-only	initial_sync_complete persisted_index_status complete published_revision git initial sync	src/mindroom/api/knowledge.py:285; src/mindroom/knowledge/manager.py:808; src/mindroom/knowledge/manager.py:1008; src/mindroom/knowledge/refresh_runner.py:715; tests/api/test_knowledge_api.py:484
_indexed_count_for_state	function	lines 39-49	related-only	indexed_count state.status complete settings compatible published_indexed_count	src/mindroom/knowledge/registry.py:772; src/mindroom/knowledge/refresh_runner.py:737; src/mindroom/knowledge/refresh_runner.py:745; tests/test_knowledge_manager.py:4711
get_knowledge_index_status	function	lines 52-84	related-only	get_knowledge_index_status load_published_index_state published_index_refresh_state availability metadata_exists	src/mindroom/api/knowledge.py:233; src/mindroom/knowledge/registry.py:280; src/mindroom/knowledge/registry.py:353; src/mindroom/knowledge/registry.py:567; src/mindroom/api/knowledge.py:515; src/mindroom/api/knowledge.py:657
mark_knowledge_source_changed_async	async_function	lines 87-102	duplicate-found	mark_knowledge_source_changed_async mark_knowledge_source_changed source changed stale async wrapper	src/mindroom/knowledge/registry.py:856; src/mindroom/knowledge/registry.py:876; src/mindroom/api/knowledge.py:190; src/mindroom/knowledge/watch.py:243; src/mindroom/knowledge/refresh_runner.py:564
```

## Findings

### Facade async stale marker duplicates the registry wrapper

`src/mindroom/knowledge/status.py:87` defines `mark_knowledge_source_changed_async` as a direct pass-through to `registry.mark_knowledge_source_changed_async`.
The target behavior lives in `src/mindroom/knowledge/registry.py:876`, which runs `mark_knowledge_source_changed` from `src/mindroom/knowledge/registry.py:856` off the event loop.
Callers in `src/mindroom/api/knowledge.py:190`, `src/mindroom/knowledge/watch.py:243`, and `src/mindroom/knowledge/refresh_runner.py:564` use stale marking after source mutations, filesystem events, and refresh decisions.

Why this is duplicated: the facade wrapper has the same signature defaults and returns the same tuple as the registry async function.
Difference to preserve: the facade appears intentional because `registry.py:4` says callers should prefer focused facades such as `mindroom.knowledge.status` instead of importing registry directly.

### Indexed-count derivation partially overlaps registry persisted-count helper

`src/mindroom/knowledge/status.py:39` returns zero unless a persisted state is complete and compatible with the current indexing settings, otherwise it returns `state.indexed_count or 0`.
`src/mindroom/knowledge/registry.py:772` has the narrower `published_indexed_count(index)` helper that returns `index.state.indexed_count or 0` for an already published handle.
`src/mindroom/knowledge/refresh_runner.py:737` and `src/mindroom/knowledge/refresh_runner.py:745` repeat the terminal `updated_state.indexed_count or 0` expression for refresh results.

Why this is not a strong duplicate: `status._indexed_count_for_state` accepts a possibly missing raw state and validates both completion and settings compatibility before exposing a count.
The registry helper only reads a count from an already valid `PublishedIndexHandle`, so extracting a shared helper would either be too narrow or would pull UI status semantics into registry.

## Proposed Generalization

No refactor recommended.
The exact wrapper duplication is a deliberate facade boundary, and the count helper carries status-specific validation that is not shared by the registry handle helper.

If this area is revisited anyway, the smallest possible cleanup would be to add a registry helper named `published_indexed_count_for_state(key, state)` only if another non-status caller needs the same complete-and-compatible gating.
That helper should preserve the current behavior for missing state, non-complete state, and incompatible indexing settings.

## Risk/tests

Changing the stale-marker facade would risk widening imports from `registry.py` into API and watcher code, contrary to the registry module guidance.
Changing indexed-count derivation could affect dashboard/API fields in `src/mindroom/api/knowledge.py:539`, `src/mindroom/api/knowledge.py:678`, and error payloads around `src/mindroom/api/knowledge.py:725`.
Relevant tests to run for any future refactor are `tests/test_knowledge_manager.py` cases covering `published_index_refresh_state`, source-change marking, and `published_indexed_count`, plus `tests/api/test_knowledge_api.py` cases covering knowledge status and git initial sync.
