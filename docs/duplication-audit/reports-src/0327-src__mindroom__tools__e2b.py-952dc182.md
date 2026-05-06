# Duplication Audit: `src/mindroom/tools/e2b.py`

## Summary

The only behavior in this module is the `e2b_tools` toolkit factory.
It duplicates the common MindRoom tool-registration shape used by many `src/mindroom/tools/*` modules: a metadata decorator plus a lazy local import that returns an Agno toolkit class.
This is real pattern duplication, but it is intentionally local, declarative, and low-risk; no refactor is recommended from this file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
e2b_tools	function	lines 69-73	related-only	e2b_tools; E2BTools; lazy import Agno toolkit factory; register_tool_with_metadata factory returning toolkit class	src/mindroom/tools/daytona.py:185; src/mindroom/tools/file_generation.py:70; src/mindroom/tools/csv.py:91; src/mindroom/tools/docker.py:52; src/mindroom/tools/__init__.py:58; src/mindroom/tool_system/metadata.py:800
```

## Findings

No meaningful duplication found that warrants a refactor.

`src/mindroom/tools/e2b.py:69` returns `E2BTools` via a lazy import at `src/mindroom/tools/e2b.py:71`.
The same factory behavior appears across many tool modules, including `src/mindroom/tools/daytona.py:185`, `src/mindroom/tools/file_generation.py:70`, `src/mindroom/tools/csv.py:91`, and `src/mindroom/tools/docker.py:52`.
These functions are functionally alike because they defer importing optional Agno toolkit dependencies until the registered factory is called, then return the toolkit class consumed by the registry.

The important differences are the imported toolkit class, return annotation, docstring, and per-tool metadata in the decorator.
`src/mindroom/tool_system/metadata.py:800` already centralizes the shared registration behavior, and `src/mindroom/tools/__init__.py:58` exposes `e2b_tools` consistently with the other tool factories.

## Proposed Generalization

No refactor recommended.

A generic lazy toolkit factory helper could remove two lines from many modules, but it would make type annotations and direct imports less explicit while providing little behavior reduction.
If this pattern grows into generated metadata or repeated validation logic later, the better location would be `src/mindroom/tool_system/metadata.py` because that module already owns tool registration and factory metadata.

## Risk/Tests

No production code was changed.
If this pattern were generalized in the future, tests should cover tool registry loading, optional dependency import timing, and representative tool instantiation for at least `e2b`, `daytona`, and one no-config local toolkit such as `docker`.
