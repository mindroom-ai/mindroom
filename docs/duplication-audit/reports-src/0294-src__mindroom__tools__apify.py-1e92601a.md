## Summary

The main duplication around `src/mindroom/tools/apify.py` is the repeated metadata-decorated lazy Agno toolkit factory shape used by many tool modules.
Apify also overlaps functionally with several web scraping and crawling tool registrations, but those wrappers expose distinct providers, config fields, dependencies, and function names, so this is a catalog-level relationship more than a safe local dedupe target.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
apify_tools	function	lines 46-50	duplicate-found	apify_tools; ApifyTools; def *_tools lazy agno import return; web scraping crawling data extraction	src/mindroom/tools/firecrawl.py:105; src/mindroom/tools/spider.py:78; src/mindroom/tools/brightdata.py:98; src/mindroom/tools/crawl4ai.py:98; src/mindroom/tools/__init__.py:28
```

## Findings

### Repeated lazy Agno toolkit factory pattern

- `src/mindroom/tools/apify.py:46` defines `apify_tools()`, imports `ApifyTools` inside the function, and returns the toolkit class.
- `src/mindroom/tools/firecrawl.py:105`, `src/mindroom/tools/spider.py:78`, `src/mindroom/tools/brightdata.py:98`, and `src/mindroom/tools/crawl4ai.py:98` follow the same behavior: a metadata-decorated `*_tools()` function does a local Agno toolkit import and returns the class.
- The behavior is duplicated because each wrapper exists primarily to defer optional dependency imports until the tool is selected while giving the registry a stable callable.
- Differences to preserve: each module imports a different Agno class and has provider-specific metadata, dependencies, docs URL, and function names.

### Related web scraping and crawling catalog entries

- `src/mindroom/tools/apify.py:15` describes web scraping, crawling, data extraction, and automation via Apify Actors.
- `src/mindroom/tools/firecrawl.py:16`, `src/mindroom/tools/spider.py:16`, `src/mindroom/tools/brightdata.py:16`, and `src/mindroom/tools/crawl4ai.py:16` register adjacent web scraping/crawling/data extraction capabilities.
- This is related behavior rather than direct duplication: the user-facing category is similar, but the wrappers configure different providers and expose different Agno tool functions.

## Proposed Generalization

No refactor recommended for this file alone.

A possible future cleanup would be a tiny helper for class-returning lazy toolkit factories, but it would touch many tool modules and may reduce the current explicitness of optional imports.
The web-scraping provider overlap should stay as separate registrations unless the product needs a shared comparison layer or category metadata normalization.

## Risk/tests

Risk is low because no production code was changed.
If a future dedupe is attempted, tests should cover tool registry metadata discovery, optional dependency behavior for missing packages, and that configured tool names still resolve through `src/mindroom/tools/__init__.py`.
