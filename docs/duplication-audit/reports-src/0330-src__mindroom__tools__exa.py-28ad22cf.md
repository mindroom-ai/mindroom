## Summary

The only behavior symbol in `src/mindroom/tools/exa.py` is `exa_tools`, and its lazy-import-and-return toolkit factory pattern is duplicated across many `src/mindroom/tools/*` modules.
This is active structural duplication, but it is also the current tool registry convention, and the per-tool decorators carry provider-specific metadata.
No Exa-specific duplicated search or content behavior was found elsewhere in `./src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
exa_tools	function	lines 196-200	duplicate-found	exa_tools, ExaTools, def *_tools() -> type, lazy import return Toolkit, search tool factories	src/mindroom/tools/tavily.py:133-137; src/mindroom/tools/duckduckgo.py:98-102; src/mindroom/tools/serper.py:98-102; src/mindroom/tools/__init__.py:61; src/mindroom/tools/__init__.py:145-181
```

## Findings

1. Registered Agno toolkit factory bodies are repeated across tool modules.

`src/mindroom/tools/exa.py:196-200` defines `exa_tools`, performs a local import of `agno.tools.exa.ExaTools`, and returns the imported toolkit class.
The same behavior appears in sibling tool modules, for example `src/mindroom/tools/tavily.py:133-137`, `src/mindroom/tools/duckduckgo.py:98-102`, and `src/mindroom/tools/serper.py:98-102`.
These functions differ only in the imported Agno toolkit class and docstring, while their runtime behavior is the same: defer optional dependency import until the registered factory is invoked and return the toolkit type.

The surrounding decorators are intentionally provider-specific.
`exa` has unique config fields, dependency `exa_py`, docs URL, and function names at `src/mindroom/tools/exa.py:13-194`, so the duplicated behavior is limited to the factory body, not the metadata.

2. Tool registry exposure repeats the same manual import/export flow.

`src/mindroom/tools/__init__.py:61` imports `exa_tools`, and `src/mindroom/tools/__init__.py:145-181` manually includes it in `__all__`.
The same pattern is used for the other registered tool factories.
This is related registry boilerplate rather than a duplicate of Exa behavior, but it reinforces that `exa_tools` follows a broad module pattern.

## Proposed Generalization

No refactor recommended for `exa_tools` alone.
The duplicated factory body is small, explicit, and tied to module-level decorators that are easy to inspect.

If the project chooses to reduce this boilerplate across all tool modules later, the minimal helper would be a tiny factory builder in `mindroom.tool_system.metadata` or a new focused helper such as `mindroom.tool_system.tool_factory`.
It could accept an import path and class name and return a callable that lazily imports the toolkit type.
That broader change should be done across many tool modules at once, with care to preserve function names, metadata registration timing, type-checking imports, and optional dependency behavior.

## Risk/tests

The main risk of generalizing this pattern is changing registration timing or optional dependency import timing.
Tests should verify that importing `mindroom.tools` still registers metadata without importing optional Agno dependencies, and that configured tools still instantiate through the existing registry path.
No production code was edited for this audit.
