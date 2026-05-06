## Summary

Top duplication candidate: `serper_tools` repeats the common MindRoom tool module behavior of registering provider metadata and returning a lazily imported Agno toolkit class.
The closest behavioral matches are other search/scrape provider modules such as `serpapi_tools`, `tavily_tools`, and `firecrawl_tools`.
This is real boilerplate duplication, but no refactor is recommended from this file alone because the provider-specific decorator metadata is the meaningful part of each module and the factory body is intentionally tiny.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
serper_tools	function	lines 98-102	duplicate-found	serper_tools; SerperTools; def *_tools returns Agno toolkit class; search_web/search_news/search_scholar/scrape_webpage; API-key search provider modules	src/mindroom/tools/serpapi.py:56; src/mindroom/tools/tavily.py:133; src/mindroom/tools/firecrawl.py:105; src/mindroom/tools/googlesearch.py:91; src/mindroom/tools/spider.py:78; src/mindroom/tools/searxng.py:56
```

## Findings

### Registered Agno toolkit factory boilerplate

- `src/mindroom/tools/serper.py:98` defines `serper_tools`, lazily imports `agno.tools.serper.SerperTools`, and returns that toolkit class.
- `src/mindroom/tools/serpapi.py:56` defines `serpapi_tools`, lazily imports `agno.tools.serpapi.SerpApiTools`, and returns that toolkit class.
- `src/mindroom/tools/tavily.py:133` defines `tavily_tools`, lazily imports `agno.tools.tavily.TavilyTools`, and returns that toolkit class.
- `src/mindroom/tools/firecrawl.py:105` defines `firecrawl_tools`, lazily imports `agno.tools.firecrawl.FirecrawlTools`, and returns that toolkit class.

These functions perform the same runtime behavior: defer importing an optional Agno toolkit until the registered factory is used, then return the toolkit class for the tool loader to instantiate/configure.
The same pattern appears across many `src/mindroom/tools/*` modules, especially for API-key-backed integrations.

Differences to preserve:

- Each module has provider-specific `register_tool_with_metadata` values, dependencies, docs URL, config fields, and function names.
- `src/mindroom/tools/googlesearch.py:91` is related but not a direct duplicate because it creates a subclass to force `backend="google"` before returning it.
- Search/scrape capability overlap is intentional across providers: Serper, SerpApi, Tavily, Firecrawl, Spider, SearxNG, DuckDuckGo, and Google Search expose similar user-facing web search behavior, but they wrap different backends and config surfaces.

## Proposed Generalization

No refactor recommended for this module alone.

A possible future cleanup would be a small helper in `mindroom.tool_system.metadata` or a nearby tool registration helper that builds these lazy Agno class factories from an import path and class name.
That would only be worthwhile if applied broadly to the tool registry because the current duplicated function body is two lines and the module-level decorator metadata remains provider-specific.

## Risk/Tests

Risk is mostly import timing and optional dependency behavior.
Any future helper must preserve lazy imports so unavailable optional tool dependencies do not break module import or metadata discovery.
It must also preserve the returned class identity expected by the existing tool loader.

Relevant tests would need to cover:

- Metadata discovery for `serper`, `serpapi`, `tavily`, and `firecrawl`.
- Factory invocation still returns the same Agno toolkit class when dependencies are installed.
- Missing optional dependency behavior remains unchanged during ordinary metadata import.
