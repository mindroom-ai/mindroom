Summary: No meaningful duplication found for the required behavior symbol.
The `firecrawl_tools` function is the standard MindRoom tool registry factory: it lazily imports the Agno toolkit class and returns that class.
Related web scraping tool modules use the same factory pattern and similar metadata fields, but that is repository-wide registration boilerplate rather than duplicated Firecrawl-specific behavior worth extracting from this single module.

Coverage TSV:

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
firecrawl_tools	function	lines 105-109	related-only	firecrawl_tools; FirecrawlTools; def .*_tools; web scraping crawl scrape search metadata factory	src/mindroom/tools/firecrawl.py:105; src/mindroom/tools/crawl4ai.py:98; src/mindroom/tools/spider.py:78; src/mindroom/tools/scrapegraph.py:91; src/mindroom/tools/__init__.py:66
```

Findings:

No real duplication requiring refactor was found.

Related-only pattern: `src/mindroom/tools/firecrawl.py:105` has the same lazy-import-and-return toolkit factory shape as `src/mindroom/tools/crawl4ai.py:98`, `src/mindroom/tools/spider.py:78`, and `src/mindroom/tools/scrapegraph.py:91`.
The behavior is intentionally uniform: each registered tool module exposes a small factory function that returns the concrete Agno toolkit class while keeping optional dependencies out of module import time.
The differences to preserve are the concrete toolkit import path, return type, metadata values, dependencies, config fields, and `function_names`.

The metadata fields around Firecrawl also overlap with other web scraping tools, especially `api_key`, `enable_scrape`, `enable_crawl`, `enable_search`, `all`, `docs_url`, and `function_names`.
Those fields describe different third-party toolkit constructors and supported tool functions, so extracting them from this single module would likely obscure provider-specific configuration without reducing active behavioral duplication.

Proposed generalization:

No refactor recommended.
If many tool modules are refactored together later, a small helper for declarative toolkit factories could reduce repeated lazy-import boilerplate across `src/mindroom/tools/*.py`, but that would be a broad registry-style cleanup and is outside this focused audit.

Risk/tests:

No production code was changed.
If a future registry helper is introduced, tests should cover metadata registration, optional dependency import behavior, and loading Firecrawl without importing `agno.tools.firecrawl` until the factory is called.
