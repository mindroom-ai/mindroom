## Summary

`linkup_tools` repeats the repository-wide Agno toolkit factory pattern: a registered tool function lazily imports one toolkit class and returns the class unchanged.
This pattern is duplicated across many `src/mindroom/tools/*.py` modules, including search and scraping providers.
No focused refactor is recommended for this file alone because the duplication is shallow, explicit, and tied to provider-specific metadata decorators.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
linkup_tools	function	lines 63-67	duplicate-found	linkup_tools, LinkupTools, def .*_tools(), Return .* tools, agno.tools.* lazy import	src/mindroom/tools/tavily.py:133, src/mindroom/tools/serpapi.py:56, src/mindroom/tools/serper.py:98, src/mindroom/tools/brightdata.py:98, src/mindroom/tools/firecrawl.py:105, src/mindroom/tools/duckduckgo.py:98
```

## Findings

### Repeated registered Agno toolkit factory

- `src/mindroom/tools/linkup.py:63` defines `linkup_tools`, imports `LinkupTools` inside the function, and returns the toolkit class unchanged.
- `src/mindroom/tools/tavily.py:133`, `src/mindroom/tools/serpapi.py:56`, `src/mindroom/tools/serper.py:98`, `src/mindroom/tools/brightdata.py:98`, `src/mindroom/tools/firecrawl.py:105`, and `src/mindroom/tools/duckduckgo.py:98` use the same behavior with different Agno toolkit classes.

The duplicated behavior is functionally the same: each factory exists so `register_tool_with_metadata` can register a callable that defers the optional Agno import until the toolkit is actually loaded, then returns the toolkit class.
The provider-specific differences to preserve are the imported Agno class, return type annotation, docstring, decorator metadata, dependencies, docs URL, and function names.

## Proposed Generalization

No refactor recommended for this file alone.
A shared helper such as `make_agno_tool_factory(module_path: str, class_name: str)` could reduce the repeated lazy import function bodies across tool modules, but it would also make type annotations, direct imports, and grep-friendly provider wiring less explicit.
If this pattern is refactored later, the helper should live near `mindroom.tool_system.metadata` or a new small `mindroom.tool_system.factories` module and should be applied mechanically across many tool wrappers in one tested change.

## Risk/tests

The main risk of generalizing is breaking optional dependency behavior by importing Agno provider modules too early.
Tests should verify that tool metadata registration still succeeds without optional provider packages installed, and that resolving each registered provider returns the expected toolkit class only when loaded.
