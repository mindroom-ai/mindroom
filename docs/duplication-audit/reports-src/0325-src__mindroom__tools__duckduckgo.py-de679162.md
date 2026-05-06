# Summary

Top duplication candidate: `duckduckgo_tools` repeats the common tool-module factory behavior used across many `src/mindroom/tools/*` modules: a decorated function lazily imports one Agno toolkit class and returns it unchanged.
This is real behavioral duplication, but it is intentionally small and tied to per-tool metadata registration, so no refactor is recommended for this file alone.
DuckDuckGo's configurable search/news surface also closely overlaps `googlesearch`, but that overlap is in module-level metadata and the Google wrapper subclass, not in the required `duckduckgo_tools` function body.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
duckduckgo_tools	function	lines 98-102	duplicate-found	"DuckDuckGoTools"; "duckduckgo_tools"; "from agno.tools.* import *Tools"; "return *Tools"; "web_search search_news enable_search enable_news fixed_max_results verify_ssl"	src/mindroom/tools/wikipedia.py:42-46; src/mindroom/tools/baidusearch.py:84-88; src/mindroom/tools/linear.py:44-48; src/mindroom/tools/googlesearch.py:91-125; src/mindroom/tools/__init__.py:57; src/mindroom/tools/__init__.py:177
```

# Findings

## Repeated lazy Agno toolkit factory pattern

`src/mindroom/tools/duckduckgo.py:98-102` defines `duckduckgo_tools`, imports `DuckDuckGoTools` inside the function, and returns that class unchanged.
The same behavior appears in many tool modules, including `src/mindroom/tools/wikipedia.py:42-46`, `src/mindroom/tools/baidusearch.py:84-88`, and `src/mindroom/tools/linear.py:44-48`.
The shared behavior is functionally the same: avoid importing optional Agno tool dependencies until the registered factory is called, then expose the toolkit class to the tool registry.
Differences to preserve are only the imported module path, returned class, type annotation, and tool-specific metadata on the decorator.

## Related search-tool metadata overlap

`src/mindroom/tools/duckduckgo.py:22-96` and `src/mindroom/tools/googlesearch.py:22-89` share many config fields and the same advertised function names, including `enable_search`, `enable_news`, `modifier`, `fixed_max_results`, `proxy`, `timeout`, `verify_ssl`, `timelimit`, `region`, and `("web_search", "search_news")`.
`src/mindroom/tools/googlesearch.py:91-125` returns a local subclass that pins `backend="google"`, while DuckDuckGo returns Agno's `DuckDuckGoTools` directly.
This is related duplication around search-tool registration, but it is outside the required function body and has provider-specific differences.

# Proposed Generalization

No refactor recommended for this primary file.
A possible future cleanup, if many tool modules are being edited together, would be a tiny helper or decorator-support path that builds lazy toolkit factories from an import path and class name.
That helper would need to preserve per-tool annotations, lazy optional dependency imports, metadata registration timing, and static type clarity, so the maintenance payoff is not strong enough for `duckduckgo_tools` alone.

# Risk/Tests

Changing this pattern could break optional dependency isolation by importing Agno toolkit modules too early.
It could also make metadata registration less explicit or weaken type checking for individual factories.
If a shared lazy factory helper is introduced later, tests should cover registry loading, importing without optional dependencies installed until factory call time, and at least one direct factory call for a simple returned class such as DuckDuckGo or Wikipedia.
