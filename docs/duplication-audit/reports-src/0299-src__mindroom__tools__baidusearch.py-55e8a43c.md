Summary: No meaningful Baidu-specific duplication found. The only behavior symbol is a small lazy-import factory that matches the convention used by many Agno toolkit registration modules, but the duplicated behavior is generic registry boilerplate rather than duplicated Baidu search logic.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
baidusearch_tools	function	lines 84-88	related-only	baidusearch_tools; BaiduSearchTools; def .*_tools; search tool lazy import factories	src/mindroom/tools/baidusearch.py:84; src/mindroom/tools/duckduckgo.py:98; src/mindroom/tools/searxng.py:56; src/mindroom/tools/serper.py:98; src/mindroom/tools/serpapi.py:56; src/mindroom/tools/googlesearch.py:91
```

## Findings

No real Baidu-specific duplicated behavior was found elsewhere under `src`.

Related boilerplate exists across tool modules: `src/mindroom/tools/baidusearch.py:84`, `src/mindroom/tools/duckduckgo.py:98`, `src/mindroom/tools/searxng.py:56`, `src/mindroom/tools/serper.py:98`, and `src/mindroom/tools/serpapi.py:56` all define a registered `*_tools()` function that lazily imports an Agno toolkit class and returns it.
This is functionally similar, but it is the established plugin-registration shape for individual tool modules.
Each module also has distinct metadata, dependencies, function names, config fields, and documentation URLs, so extracting the tiny factory body alone would add indirection without removing meaningful domain duplication.

`src/mindroom/tools/googlesearch.py:91` is related but not equivalent: it lazily imports `WebSearchTools`, defines a wrapper subclass to pin the `backend="google"` argument, and returns that subclass.
That difference should be preserved if any future registry boilerplate is generalized.

## Proposed Generalization

No refactor recommended.

If the project later decides to reduce boilerplate across all simple Agno toolkit modules, the smallest reasonable abstraction would be a metadata helper in `src/mindroom/tool_system/metadata.py` or a focused tool-module helper that accepts an import path and class name and returns a lazy factory.
That should be considered only as a broad tool-registry cleanup, not as a change motivated by `baidusearch_tools()` alone.

## Risk/Tests

No production change is recommended, so no tests are required for this audit.

If a future refactor generalizes lazy toolkit factories, tests should cover tool registration discovery, dependency metadata preservation, and instantiation of representative simple and custom-wrapper modules, including `baidusearch`, `duckduckgo`, `searxng`, and `googlesearch`.
