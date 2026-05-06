Summary: The primary file contains one tool-registration factory.
Its lazy import and return-a-toolkit-class behavior is duplicated across many `src/mindroom/tools/*` configuration modules, including adjacent browser/web-scraping tools.
This is an intentional registry convention with tool-specific metadata, so no refactor is recommended from this file alone.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
browser_tools	function	lines 46-50	duplicate-found	"browser_tools", "def web_browser_tools", "def browserbase_tools", "Return .* tools", "from .* import .*Tools", "return .*Tools"	src/mindroom/tools/web_browser_tools.py:42, src/mindroom/tools/browserbase.py:107, src/mindroom/tools/website.py:35, src/mindroom/tools/coding.py:52, src/mindroom/tools/attachments.py:38
```

Findings:

1. Registered lazy toolkit factory pattern is repeated.
   `src/mindroom/tools/browser.py:46` defines `browser_tools()`, imports `BrowserTools` inside the function, and returns the class.
   The same behavior appears in `src/mindroom/tools/web_browser_tools.py:42`, `src/mindroom/tools/browserbase.py:107`, `src/mindroom/tools/website.py:35`, `src/mindroom/tools/coding.py:52`, and `src/mindroom/tools/attachments.py:38`.
   These functions all serve as zero-argument registry factories that avoid runtime imports until the tool is requested.
   Differences to preserve are the concrete toolkit class, the `TYPE_CHECKING` import target, and each module's `register_tool_with_metadata(...)` arguments.

Proposed generalization:

No refactor recommended.
The duplicated body is only a local import plus `return` and sits next to highly tool-specific metadata.
A shared factory helper could reduce a few lines per module, but it would make import paths more indirect without materially reducing behavioral complexity.

Risk/tests:

Any future refactor of this pattern would need coverage for registry discovery and lazy dependency behavior, especially that optional tool dependencies are not imported during module import.
Relevant tests should exercise tool metadata export and resolving the `browser` toolkit factory into `mindroom.custom_tools.browser.BrowserTools`.
