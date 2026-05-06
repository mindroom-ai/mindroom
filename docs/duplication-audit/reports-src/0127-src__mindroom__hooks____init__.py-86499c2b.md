## Summary

No meaningful duplication found.

`src/mindroom/hooks/__init__.py` is a public facade.
Its only behavior symbol, `build_hook_matrix_admin`, is an intentional lazy forwarding wrapper to `src/mindroom/hooks/matrix_admin.py` to avoid package cycles.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
build_hook_matrix_admin	function	lines 162-169	related-only	build_hook_matrix_admin, HookMatrixAdmin, hook_matrix_admin, lazy import, matrix admin	src/mindroom/hooks/matrix_admin.py:67; src/mindroom/hooks/context.py:193; src/mindroom/orchestrator.py:1005; src/mindroom/turn_controller.py:899; src/mindroom/scheduling.py:385; src/mindroom/scheduling.py:1694
```

## Findings

No real duplication to consolidate.

`src/mindroom/hooks/__init__.py:162` defines a facade-level `build_hook_matrix_admin` that imports and calls `src/mindroom/hooks/matrix_admin.py:67`.
The concrete implementation returns `_BoundHookMatrixAdmin`, while the facade wrapper exists only to keep `mindroom.hooks` exports available without importing the concrete Matrix admin module during package initialization.

Adjacent hook builders are not duplicates.
`src/mindroom/hooks/state.py:13` and `src/mindroom/hooks/state.py:45` build bound room-state closures.
`src/mindroom/hooks/sender.py:97` builds a bound message sender.
They share the broad hook-helper pattern but wrap different Matrix operations and carry different behavior.

Call sites in `src/mindroom/orchestrator.py:1005`, `src/mindroom/turn_controller.py:899`, and `src/mindroom/scheduling.py:385` reuse the public builder rather than reimplementing Matrix admin binding.

## Proposed Generalization

No refactor recommended.

The only same-name candidate is the concrete implementation that the facade intentionally delegates to.
Introducing a generic lazy-export helper would add indirection for a single current case and would not remove duplicated domain behavior.

## Risk/tests

Changing this wrapper could reintroduce hook package import cycles.
Relevant tests to keep in view are `tests/test_hook_matrix_admin.py`, `tests/test_hook_execution.py`, `tests/test_hook_schedule.py`, and `tests/test_agno_history.py`.
