## Summary

No meaningful duplication found.
`src/mindroom/knowledge/__init__.py` is a package facade that re-exports selected knowledge package symbols and defines `__all__`.
Comparable facades exist in other packages, but this is import-surface structure rather than duplicated behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-25	not-a-behavior-symbol	knowledge package __init__ exports __all__ KnowledgeManager KnowledgeAccessSupport KnowledgeRefreshScheduler	src/mindroom/knowledge/__init__.py:1; src/mindroom/memory/__init__.py:1; src/mindroom/matrix/cache/__init__.py:1; src/mindroom/hooks/__init__.py:1; src/mindroom/knowledge/utils.py:47; src/mindroom/knowledge/manager.py:788; src/mindroom/knowledge/refresh_scheduler.py:39; src/mindroom/knowledge/availability.py:8
```

## Findings

No real duplication to report.
The primary module has no parsing, validation, IO, Matrix/message/config transformation, lifecycle flow, or error handling.
Its only behavior-adjacent role is maintaining the public import surface for `mindroom.knowledge`.

Related package facades in `src/mindroom/memory/__init__.py:1`, `src/mindroom/matrix/cache/__init__.py:1`, and `src/mindroom/hooks/__init__.py:1` follow the same import-plus-`__all__` convention.
Those modules expose different domain symbols, so a shared helper would not remove runtime duplication or simplify behavior.

The exported knowledge symbols themselves are implemented in focused modules:
`KnowledgeAvailability` in `src/mindroom/knowledge/availability.py:8`, `KnowledgeManager` in `src/mindroom/knowledge/manager.py:788`, `KnowledgeRefreshScheduler` in `src/mindroom/knowledge/refresh_scheduler.py:39`, and access/notice helpers in `src/mindroom/knowledge/utils.py:47`.
The facade does not duplicate those implementations.

## Proposed Generalization

No refactor recommended.
Keep the explicit package facade because it is short, readable, and matches existing package style.

## Risk/Tests

No production-code change is recommended.
If this facade were edited later, import-surface tests or type-checking should verify that existing callers using `from mindroom.knowledge import ...` still resolve the same symbols.
