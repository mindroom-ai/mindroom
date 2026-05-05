Summary: `spider_tools` duplicates the repository-wide Agno toolkit factory pattern and overlaps with neighboring web scrape/crawl/search tool metadata.
No production refactor is recommended from this file alone because the only required behavior symbol is a small lazy import wrapper, and the overlapping web-scrape tool metadata has provider-specific defaults and function names.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
spider_tools	function	lines 78-82	related-only	spider_tools, SpiderTools, web scraping and crawling, enable_scrape, enable_crawl, search_web, def *_tools() -> type[...]	src/mindroom/tools/firecrawl.py:105, src/mindroom/tools/crawl4ai.py:98, src/mindroom/tools/brightdata.py:98, src/mindroom/tools/agentql.py:103, src/mindroom/tools/scrapegraph.py:91, src/mindroom/tools/__init__.py:115
```

Findings:

1. Metadata-decorated lazy toolkit factories are repeated across most tool modules.
   `src/mindroom/tools/spider.py:78` imports `SpiderTools` inside `spider_tools` and returns the toolkit class.
   The same behavior appears in `src/mindroom/tools/firecrawl.py:105`, `src/mindroom/tools/crawl4ai.py:98`, `src/mindroom/tools/brightdata.py:98`, and `src/mindroom/tools/scrapegraph.py:91`.
   This is functionally the same registration-time wrapper shape: keep optional Agno dependencies out of import-time execution, expose a typed factory, and let `register_tool_with_metadata` attach catalog metadata.
   The differences to preserve are the toolkit class, docstring, and any compatibility work before import, such as `src/mindroom/tools/agentql.py:105`.

2. Spider's web scrape/crawl/search capability flags overlap with other web scrape tool modules.
   `src/mindroom/tools/spider.py:45`, `src/mindroom/tools/spider.py:52`, and `src/mindroom/tools/spider.py:59` define `enable_search`, `enable_scrape`, and `enable_crawl`.
   Similar capability toggles exist in `src/mindroom/tools/firecrawl.py:31`, `src/mindroom/tools/firecrawl.py:38`, `src/mindroom/tools/firecrawl.py:52`, `src/mindroom/tools/crawl4ai.py:80`, and `src/mindroom/tools/scrapegraph.py:45`.
   The behavior is related because these toggles map provider-specific web operations into configurable tool availability.
   It is not a clean duplicate because defaults and operation names differ by provider, for example Firecrawl disables crawl/search by default while Spider enables all three.

Proposed generalization:

No refactor recommended for `spider_tools` alone.
If this pattern is tackled across the entire `src/mindroom/tools` package, the smallest useful helper would be a metadata/construction helper near `mindroom.tool_system.metadata` that builds common `ConfigField` values for capability toggles such as `enable_scrape`, `enable_crawl`, `enable_search`, and `all`.
The lazy factory wrappers should probably remain explicit unless a broad tool-registration generator is already being introduced, because a helper for a three-line function would add indirection without reducing meaningful behavior.

Risk/tests:

The main risk in generalizing toggle fields is changing provider defaults or labels, which would affect dashboard configuration and instantiated Agno toolkit behavior.
Any future refactor should compare generated tool metadata against `src/mindroom/tools_metadata.json` and cover at least Spider, Firecrawl, Crawl4AI, and ScrapeGraph metadata snapshots or API responses.
No tests were run because this task requested an audit report only and no production code edits.
