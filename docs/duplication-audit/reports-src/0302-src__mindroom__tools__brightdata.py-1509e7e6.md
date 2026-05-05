## Summary

The only behavior symbol in `src/mindroom/tools/brightdata.py` is the registered toolkit factory `brightdata_tools`.
It duplicates the common tool-module pattern used by many `src/mindroom/tools/*` modules: metadata decorator plus a tiny lazy import function returning an Agno toolkit class.
The closest functional neighbors are other web scraping and search providers (`firecrawl`, `oxylabs`, `spider`, `scrapegraph`, `browserbase`, `agentql`, `jina`, and `website`), but their provider-specific metadata and config fields differ enough that no production refactor is recommended from this file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
brightdata_tools	function	lines 98-102	related-only	brightdata_tools; BrightDataTools; def *_tools; web_scrape docs_url; scrape/search/screenshot tool modules	src/mindroom/tools/firecrawl.py:105; src/mindroom/tools/oxylabs.py:45; src/mindroom/tools/spider.py:78; src/mindroom/tools/scrapegraph.py:91; src/mindroom/tools/browserbase.py:107; src/mindroom/tools/agentql.py:103; src/mindroom/tools/jina.py:91; src/mindroom/tools/website.py:35; src/mindroom/tools/__init__.py:36
```

## Findings

No real duplication requiring consolidation was found.

`brightdata_tools` at `src/mindroom/tools/brightdata.py:98` follows the same lazy toolkit factory pattern as many registered tool modules, including `firecrawl_tools` at `src/mindroom/tools/firecrawl.py:105`, `oxylabs_tools` at `src/mindroom/tools/oxylabs.py:45`, `spider_tools` at `src/mindroom/tools/spider.py:78`, `scrapegraph_tools` at `src/mindroom/tools/scrapegraph.py:91`, `browserbase_tools` at `src/mindroom/tools/browserbase.py:107`, `jina_tools` at `src/mindroom/tools/jina.py:91`, and `website_tools` at `src/mindroom/tools/website.py:35`.
The duplicated behavior is the intentional registry boundary: a public function returns the provider toolkit class while delaying optional dependency imports until the tool is actually loaded.

The related web scraping/search modules also repeat metadata shapes such as `ToolCategory.RESEARCH`, `SetupType.API_KEY`, optional `api_key` fields, `enable_*` feature toggles, `all`, Agno web-scrape documentation URLs, and `function_names` declarations.
Those similarities are provider-catalog data rather than shared runtime logic.
Differences to preserve include BrightData-specific toggles (`enable_scrape_markdown`, `enable_screenshot`, `enable_search_engine`, `enable_web_data_feed`), zone fields (`serp_zone`, `web_unlocker_zone`), timeout defaults, dependency name (`requests`), and BrightData function names.

`agentql_tools` at `src/mindroom/tools/agentql.py:103` shows why a generic factory abstraction would need exceptions: it performs `_ensure_agentql_playwright_stealth_compat()` before importing and returning `AgentQLTools`.
That makes a blanket replacement of all simple factories less attractive unless the registry system grows a first-class declarative loader.

## Proposed Generalization

No refactor recommended.

A future low-risk cleanup could introduce tiny metadata helper constructors for repeated catalog fields such as optional password `api_key` and boolean `enable_*` config entries, but this file alone does not justify adding another abstraction.
Do not replace `brightdata_tools` with a generic import-by-string helper unless the registry can preserve typed return annotations, lazy optional dependency behavior, and per-tool pre-import hooks.

## Risk/tests

No production code was edited.

If this area is later refactored, tests should cover tool registry discovery for `brightdata`, preservation of `function_names`, config field names/defaults, and lazy import behavior when optional BrightData dependencies are absent.
Because the current duplication is mostly declarative catalog shape, the main risk of abstraction would be silently changing metadata consumed by the API/dashboard or tool loader.
