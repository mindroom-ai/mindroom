## Summary

No meaningful duplication found.
`src/mindroom/tools/serpapi.py` only registers Agno's `SerpApiTools` and returns that toolkit class.
The same metadata-registration shape appears across many tool modules, and the closest related search-provider modules are `serper`, `linkup`, `tavily`, `exa`, `duckduckgo`, and `googlesearch`, but none duplicate SerpApi-specific behavior or implement a parallel SerpApi wrapper.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
serpapi_tools	function	lines 56-60	related-only	SerpApiTools, serpapi_tools, serpapi, search_google, search_youtube, API-key search toolkit registration	src/mindroom/tools/serper.py:91; src/mindroom/tools/linkup.py:63; src/mindroom/tools/tavily.py:134; src/mindroom/tools/exa.py:196; src/mindroom/tools/duckduckgo.py:98; src/mindroom/tools/googlesearch.py:91
```

## Findings

No real duplicated SerpApi behavior was found.

The closest related code is the repeated Agno toolkit registration pattern in search-provider modules:

- `src/mindroom/tools/serper.py:91` returns `SerperTools` after registering API-key-backed Google/news/scholar search metadata.
- `src/mindroom/tools/linkup.py:63` returns `LinkupTools` after registering API-key-backed web search metadata.
- `src/mindroom/tools/tavily.py:134` returns `TavilyTools` after registering API-key-backed web search and extraction metadata.
- `src/mindroom/tools/exa.py:196` returns `ExaTools` after registering API-key-backed search, content, and research metadata.
- `src/mindroom/tools/duckduckgo.py:98` returns `DuckDuckGoTools` after registering search metadata without API-key setup.
- `src/mindroom/tools/googlesearch.py:91` returns a local `GoogleSearchTools` wrapper around `WebSearchTools` with the backend pinned to Google.

These modules share the same registration idiom, but the behavior exposed to Agno is provider-specific and configured by distinct metadata fields, dependencies, docs URLs, and function names.
For `serpapi_tools`, the executable behavior remains entirely in Agno's `SerpApiTools`, so there is no active duplicated local implementation to consolidate.

## Proposed Generalization

No refactor recommended.
The repeated two-line factory shape is intentional registry glue and extracting it would obscure provider-specific metadata without removing meaningful behavior duplication.

## Risk/tests

Changing this module would primarily risk tool registry metadata and runtime toolkit loading.
If a future refactor touches this pattern, focused tests should verify that `serpapi` metadata still exposes `search_google` and `search_youtube`, keeps the `google-search-results` dependency, and instantiates Agno's `SerpApiTools` with configured API-key fields intact.
