## Summary

No meaningful duplication found.
`src/mindroom/tools/web_browser_tools.py` is a small metadata wrapper around Agno's `WebBrowserTools`.
Its factory shape is repeated across many tool registration modules, but this appears to be the repository's intentional lazy-import registration pattern rather than duplicated domain behavior worth extracting for this file.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
web_browser_tools	function	lines 42-46	related-only	web_browser_tools, WebBrowserTools, open_page, browser_tools, WebsiteTools, Crawl4aiTools	src/mindroom/tools/browser.py:46, src/mindroom/custom_tools/browser.py:251, src/mindroom/tools/website.py:35, src/mindroom/tools/crawl4ai.py:98, src/mindroom/tools/firecrawl.py:105, src/mindroom/tools/duckduckgo.py:98
```

## Findings

No real duplication found for `web_browser_tools`.

The function at `src/mindroom/tools/web_browser_tools.py:42` lazily imports and returns `agno.tools.webbrowser.WebBrowserTools`.
Similar one-function lazy factory wrappers exist in `src/mindroom/tools/website.py:35`, `src/mindroom/tools/crawl4ai.py:98`, `src/mindroom/tools/firecrawl.py:105`, and `src/mindroom/tools/duckduckgo.py:98`.
Those factories share the same registration mechanics, but each exposes a different toolkit and metadata contract.

`src/mindroom/tools/browser.py:46` and `src/mindroom/custom_tools/browser.py:251` are related by name and user-facing domain, but they do not duplicate the same behavior.
`browser_tools` exposes MindRoom's Playwright/OpenClaw-style browser-control toolkit, while `web_browser_tools` exposes Agno's standard-library webbrowser opener with `open_page`.

## Proposed Generalization

No refactor recommended.

Extracting a generic "lazy Agno toolkit factory" would remove only two lines per module and would make the registration files less explicit.
The current repetition is small, consistent, and coupled to per-tool metadata declarations.

## Risk/tests

No production change was made.
If this area is refactored later, tests should verify that tool registry metadata still maps `web_browser_tools` to `WebBrowserTools`, preserves `function_names=("open_page",)`, and keeps user-configurable fields `enable_open_page` and `all` intact.
