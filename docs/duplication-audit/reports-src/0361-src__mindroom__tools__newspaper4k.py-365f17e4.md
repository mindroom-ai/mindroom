## Summary

The only required behavior symbol, `newspaper4k_tools`, participates in the repeated Agno tool-wrapper pattern used across `src/mindroom/tools`.
The closest behavior-level overlap is with other web content extraction wrappers, especially `trafilatura_tools`, `jina_tools`, `crawl4ai_tools`, `firecrawl_tools`, and `website_tools`.
This is related duplication rather than a refactor target for this file alone, because each wrapper registers a different upstream toolkit, dependency set, configuration surface, and function list.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
newspaper4k_tools	function	lines 56-60	related-only	newspaper4k_tools, Newspaper4kTools, register_tool_with_metadata, def *_tools() -> type[*Tools], web scraping/content extraction wrappers	src/mindroom/tools/newspaper4k.py:13, src/mindroom/tools/trafilatura.py:13, src/mindroom/tools/trafilatura.py:170, src/mindroom/tools/jina.py:13, src/mindroom/tools/jina.py:91, src/mindroom/tools/crawl4ai.py:13, src/mindroom/tools/crawl4ai.py:98, src/mindroom/tools/firecrawl.py:13, src/mindroom/tools/firecrawl.py:105, src/mindroom/tools/website.py:13, src/mindroom/tools/website.py:35, src/mindroom/tools/__init__.py:91, src/mindroom/tools/__init__.py:211
```

## Findings

No real duplication is specific enough to justify extracting code from `newspaper4k_tools`.

Related pattern: Agno toolkit wrapper registration is repeated broadly.
`src/mindroom/tools/newspaper4k.py:13` registers metadata and `src/mindroom/tools/newspaper4k.py:56` lazily imports and returns `Newspaper4kTools`.
The same wrapper shape appears in many tool modules, including `src/mindroom/tools/crawl4ai.py:13` and `src/mindroom/tools/crawl4ai.py:98`, `src/mindroom/tools/firecrawl.py:13` and `src/mindroom/tools/firecrawl.py:105`, `src/mindroom/tools/jina.py:13` and `src/mindroom/tools/jina.py:91`, `src/mindroom/tools/trafilatura.py:13` and `src/mindroom/tools/trafilatura.py:170`, and `src/mindroom/tools/website.py:13` and `src/mindroom/tools/website.py:35`.
The duplicated behavior is the registry/factory shell: attach metadata, keep upstream imports type-checking-only at module load, import the Agno toolkit inside the function, and return the toolkit class.
The differences to preserve are the per-tool metadata, dependencies, docs URL, function names, and imported toolkit class.

Related domain overlap: multiple registered tools expose web reading, scraping, crawling, or extraction.
`newspaper4k` focuses on news article extraction with `get_article_data` and `read_article`.
`trafilatura` covers general text and metadata extraction plus crawl and HTML conversion.
`jina` reads URLs through Jina Reader and can search.
`crawl4ai`, `firecrawl`, and `website` cover broader scraping/crawling or knowledge ingestion.
These tools overlap in user-facing capability but are separate upstream integrations with materially different configuration and runtime behavior.

## Proposed Generalization

No refactor recommended for `src/mindroom/tools/newspaper4k.py` in isolation.

A future broader cleanup could consider a tiny declarative helper for simple Agno wrapper modules that only differ by metadata and toolkit import path.
That should be evaluated across all wrapper modules at once, not from this single 60-line file, because the current explicit pattern is easy to scan and preserves lazy imports.

## Risk/tests

No production code was changed.
If a future generalization is attempted, the main risks are breaking lazy optional dependency imports, losing accurate metadata in `tools_metadata.json`, or changing `src/mindroom/tools/__init__.py` exports.
Tests should cover metadata registration for the affected tools, optional dependency behavior when upstream packages are missing, and import/export availability for each refactored wrapper.
