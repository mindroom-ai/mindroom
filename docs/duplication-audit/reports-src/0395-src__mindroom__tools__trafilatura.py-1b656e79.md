Summary: `trafilatura_tools` duplicates the standard built-in tool wrapper pattern used throughout `src/mindroom/tools`: register metadata at import time, lazily import the Agno toolkit class, and return that class. This is intentional catalog boilerplate, not enough by itself to justify a refactor. Web extraction/crawling behavior is related to other web-scraping tool modules, but this file only exposes Agno's `TrafilaturaTools` and does not implement extraction logic locally.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
trafilatura_tools	function	lines 170-174	duplicate-found	"def *_tools()", "TrafilaturaTools", "crawl_website", "extract_text", "web scraping", "register_tool_with_metadata"	src/mindroom/tools/website.py:35, src/mindroom/tools/jina.py:91, src/mindroom/tools/crawl4ai.py:98, src/mindroom/tools/firecrawl.py:105, src/mindroom/tools/scrapegraph.py:91, src/mindroom/tools/spider.py:78
```

## Findings

1. Repeated lazy Agno toolkit class provider

- Primary: `src/mindroom/tools/trafilatura.py:170` imports `TrafilaturaTools` inside `trafilatura_tools` and returns the class.
- Similar candidates: `src/mindroom/tools/website.py:35`, `src/mindroom/tools/jina.py:91`, `src/mindroom/tools/crawl4ai.py:98`, `src/mindroom/tools/firecrawl.py:105`, `src/mindroom/tools/scrapegraph.py:91`, and `src/mindroom/tools/spider.py:78`.
- These functions all perform the same behavior: a no-argument catalog factory lazily imports a concrete `agno.tools.*` toolkit class, then returns that class for registry instantiation elsewhere.
- Differences to preserve: each wrapper has a distinct return type, docstring, dependency list, metadata fields, setup status, and exported tool function names. The lazy import also keeps optional tool dependencies importable only when needed.

2. Related web extraction/crawling catalog surface

- Primary: `src/mindroom/tools/trafilatura.py:13` registers web-page text extraction, metadata extraction, batch extraction, HTML-to-text conversion, and crawling.
- Related candidates: `src/mindroom/tools/website.py:13`, `src/mindroom/tools/jina.py:13`, `src/mindroom/tools/crawl4ai.py:13`, `src/mindroom/tools/firecrawl.py:13`, `src/mindroom/tools/scrapegraph.py:13`, and `src/mindroom/tools/spider.py:13`.
- These modules expose overlapping user-facing capabilities around reading URLs, scraping pages, crawling sites, and converting web content into LLM-ready text.
- This is related functionality rather than duplicated local implementation: each module delegates to a different Agno toolkit/provider and has provider-specific configuration.

## Proposed generalization

No refactor recommended for `trafilatura_tools` alone.

A shared helper for lazy toolkit imports would reduce many three-line wrappers, but it would also obscure static type imports, docstrings, and the simple explicit pattern used consistently across built-in tools.
If this pattern is refactored globally later, the smallest viable helper would live in `src/mindroom/tool_system/metadata.py` or a focused `src/mindroom/tool_system/toolkit_loader.py` and accept an import path plus class name while preserving optional dependency behavior.
That should be a repository-wide cleanup, not a change driven by this module.

## Risk/tests

- Behavior risk of refactoring the wrapper pattern: breaking lazy optional dependency imports or weakening typed return annotations for registered tool providers.
- Metadata risk: accidentally changing `function_names`, `dependencies`, or config fields would alter the tool catalog and runtime tool selection.
- Tests needed for any future refactor: tool metadata export/validation tests, registry import tests for optional dependencies, and at least one runtime load test that resolves `trafilatura` without importing unrelated web-scraping packages.
