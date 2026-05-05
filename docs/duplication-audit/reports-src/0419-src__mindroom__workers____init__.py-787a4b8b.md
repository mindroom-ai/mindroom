## Summary

No meaningful duplication found.
The primary file only exposes worker package symbols through import re-exports and `__all__`.
Similar package facade patterns exist elsewhere under `src/mindroom`, but they are conventional package APIs rather than duplicated runtime behavior that should be generalized.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-15	related-only	workers package __init__ re-export __all__ WorkerBackend WorkerManager WorkerSpec worker_api_endpoint; compared package facade __init__ modules	src/mindroom/workers/__init__.py:1; src/mindroom/mcp/__init__.py:3; src/mindroom/knowledge/__init__.py:5; src/mindroom/history/__init__.py:3; src/mindroom/memory/__init__.py:3; src/mindroom/oauth/__init__.py:3; src/mindroom/workers/backends/__init__.py:1; src/mindroom/__init__.py:3; src/mindroom/config/__init__.py:1
```

## Findings

No real duplication found.

`src/mindroom/workers/__init__.py:1` through `src/mindroom/workers/__init__.py:15` is a package-level public API facade.
It imports worker abstractions from `backend.py`, `manager.py`, and `models.py`, then lists those names in `__all__`.

Related facade patterns appear in `src/mindroom/mcp/__init__.py:3`, `src/mindroom/knowledge/__init__.py:5`, `src/mindroom/history/__init__.py:3`, `src/mindroom/memory/__init__.py:3`, and `src/mindroom/oauth/__init__.py:3`.
Those files perform the same broad package-export role, but each one names a different package-specific public surface.
There is no shared parsing, validation, IO, Matrix transformation, worker lifecycle logic, or error-handling behavior duplicated by this module.

## Proposed Generalization

No refactor recommended.

Automating `__all__` construction or introducing a shared package facade helper would add indirection for a static public API list and would not reduce meaningful behavior duplication.

## Risk/tests

Risk is limited to public import compatibility.
If this facade changes, tests should verify imports such as `from mindroom.workers import WorkerManager, WorkerSpec, worker_api_endpoint`.
No production code was edited for this audit.
