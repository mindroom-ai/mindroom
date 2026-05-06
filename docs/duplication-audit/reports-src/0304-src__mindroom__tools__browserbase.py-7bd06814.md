## Summary

`browserbase_tools` duplicates the repository-wide tool factory pattern: a registered zero-argument function lazily imports one toolkit class and returns it.
This is repeated across many `src/mindroom/tools/*.py` modules, including nearby web/scraping tools such as `firecrawl_tools`, `crawl4ai_tools`, `website_tools`, and `web_browser_tools`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
browserbase_tools	function	lines 107-111	duplicate-found	"browserbase_tools", "BrowserbaseTools", "from agno.tools.* import *Tools", "return *Tools", "def *_tools() -> type"	src/mindroom/tools/browserbase.py:107; src/mindroom/tools/firecrawl.py:105; src/mindroom/tools/crawl4ai.py:98; src/mindroom/tools/website.py:35; src/mindroom/tools/web_browser_tools.py:42; src/mindroom/tools/browser.py:46
```

## Findings

### Repeated lazy toolkit class factories

- `src/mindroom/tools/browserbase.py:107` defines `browserbase_tools`, imports `BrowserbaseTools` inside the function, and returns that class at `src/mindroom/tools/browserbase.py:111`.
- `src/mindroom/tools/firecrawl.py:105` does the same for `FirecrawlTools`, returning it at `src/mindroom/tools/firecrawl.py:109`.
- `src/mindroom/tools/crawl4ai.py:98` does the same for `Crawl4aiTools`, returning it at `src/mindroom/tools/crawl4ai.py:102`.
- `src/mindroom/tools/website.py:35` does the same for `WebsiteTools`, returning it at `src/mindroom/tools/website.py:39`.
- `src/mindroom/tools/web_browser_tools.py:42` does the same for `WebBrowserTools`, returning it at `src/mindroom/tools/web_browser_tools.py:46`.

The duplicated behavior is not browser automation itself.
It is the registration factory shape used to delay optional dependency imports until the tool is selected.
The modules differ in metadata, config fields, dependencies, docs URLs, and toolkit class names.
Those differences should remain local to each tool definition.

`src/mindroom/tools/browser.py:46` is related but not an Agno external toolkit wrapper.
It returns `mindroom.custom_tools.browser.BrowserTools` at `src/mindroom/tools/browser.py:50`, so it shares the factory shape but not the optional external dependency concern.

## Proposed generalization

No refactor recommended for this single file.

A possible future cleanup, if applied broadly, would be a small helper in `src/mindroom/tool_system/metadata.py` or a focused registry helper that builds zero-argument toolkit factories from an import path and class name.
That would only be worthwhile if it can preserve lazy imports, typing clarity, decorator metadata locality, and readable per-tool modules without making the registration flow more indirect.

## Risk/tests

The main behavior risk in any refactor is importing optional tool dependencies eagerly, which would break environments that have not installed every optional toolkit package.
Tests should cover registry import of `mindroom.tools`, metadata export, and selecting a tool with missing optional dependencies.
No production code was changed for this audit.
