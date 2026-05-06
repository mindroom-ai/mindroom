## Summary

No meaningful duplication found in `src/mindroom/tools/website.py`.
The only behavior symbol is a thin metadata-registered factory for Agno `WebsiteTools`.
Several other tool modules expose related web scraping, crawling, or URL-reading capabilities, but they wrap different upstream toolkits with different setup and configuration surfaces.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
website_tools	function	lines 35-39	related-only	website_tools WebsiteTools read_url add_website_to_knowledge web scraping content extraction crawl_website scrape_website read_url	src/mindroom/tools/jina.py:13,91; src/mindroom/tools/trafilatura.py:13,170; src/mindroom/tools/firecrawl.py:13,105; src/mindroom/tools/newspaper4k.py:13,56; src/mindroom/tools/serper.py:13,98; src/mindroom/tools/crawl4ai.py:13,98; src/mindroom/tools/agentql.py:53,103; src/mindroom/tools/web_browser_tools.py:13,42
```

## Findings

No real duplication requiring refactor was found for `website_tools`.

Related behavior exists across web-oriented tool registrations:

- `src/mindroom/tools/website.py:13` registers a no-config research toolkit for website content extraction with `read_url` and `add_website_to_knowledge`.
- `src/mindroom/tools/jina.py:13` registers URL reading through Jina with `read_url`, but it requires API-oriented configuration such as API key, base URL, search URL, max content length, timeout, and feature toggles.
- `src/mindroom/tools/trafilatura.py:13` registers local extraction and crawling with functions such as `extract_text`, `html_to_text`, and `crawl_website`, but its configuration is extraction-format and crawling specific.
- `src/mindroom/tools/firecrawl.py:13`, `src/mindroom/tools/serper.py:13`, and `src/mindroom/tools/agentql.py:53` register web scraping functions, but each is API-key backed and exposes provider-specific controls.
- `src/mindroom/tools/crawl4ai.py:13` registers crawling behavior, but it is crawl-focused rather than the simple website read/add-to-knowledge flow.
- `src/mindroom/tools/newspaper4k.py:13` overlaps only for article extraction, with news-specific functions and options.
- `src/mindroom/tools/web_browser_tools.py:13` shares the URL domain only; it opens pages and does not extract page content.

The `website_tools` function body itself is structurally similar to many other tool factories, but that repetition is the repository's current registration pattern rather than duplicated website behavior.

## Proposed Generalization

No refactor recommended.

A shared helper for metadata-registered Agno factory functions would need to encode imports, type-checking, decorator arguments, docs URLs, dependencies, and function names across many modules.
That would be broader than this module's duplication signal and would not simplify the web extraction behavior itself.

## Risk/Tests

No production code was changed.

If a future refactor consolidates web extraction tool discovery, tests should cover tool metadata registration, available function names, setup requirements, and configuration fields for `website`, `jina`, `trafilatura`, `firecrawl`, `serper`, `crawl4ai`, `agentql`, and `newspaper`.
