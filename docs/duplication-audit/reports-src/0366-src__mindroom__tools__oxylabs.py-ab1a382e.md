Summary: No meaningful duplication found.

`src/mindroom/tools/oxylabs.py` follows the repository's repeated Agno toolkit registration pattern: metadata on a module-level factory plus a lazy import that returns the toolkit class.
The `oxylabs_tools` function is mechanically similar to many other `src/mindroom/tools/*` factories, but the shared behavior is only provider registration boilerplate and each module carries provider-specific metadata, dependencies, docs, and config fields.
No refactor is recommended for this primary file alone.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
oxylabs_tools	function	lines 45-49	related-only	"def *_tools", "from agno.tools", "web_scrape", "OxylabsTools", lazy import returns toolkit class	src/mindroom/tools/brightdata.py:98, src/mindroom/tools/browserbase.py:107, src/mindroom/tools/firecrawl.py:105, src/mindroom/tools/spider.py:78, src/mindroom/tools/newspaper4k.py:56, src/mindroom/tools/website.py:35, src/mindroom/tools/crawl4ai.py:98
```

Findings:

No real duplication requiring consolidation was found for `oxylabs_tools`.
The function at `src/mindroom/tools/oxylabs.py:45` lazily imports and returns `agno.tools.oxylabs.OxylabsTools`.
That is the same registration-factory shape used by other Agno wrappers such as `src/mindroom/tools/brightdata.py:98`, `src/mindroom/tools/browserbase.py:107`, `src/mindroom/tools/firecrawl.py:105`, `src/mindroom/tools/spider.py:78`, and `src/mindroom/tools/newspaper4k.py:56`.
The duplicated part is only the two-line lazy import/return idiom.
The observable behavior differs by toolkit class and by the decorator metadata above each factory, especially config fields, dependency packages, function names, docs URLs, status, and setup type.

Proposed generalization:

No refactor recommended.
A generic helper such as `lazy_toolkit_factory(import_path, class_name)` could reduce many two-line factories, but it would add indirection across the tool registry and complicate static typing for a very small amount of repeated code.
A metadata-driven generator would be broader than the active duplication in this file and should only be considered if the project intentionally redesigns all Agno toolkit registration modules together.

Risk/tests:

No production code was changed.
If a future refactor centralizes lazy toolkit loading, tests should cover metadata registration, dependency lookup, function name exposure, and importing optional Agno toolkit packages only when the factory is called.
For Oxylabs specifically, a focused test would assert that the `oxylabs` registry entry preserves username/password config fields and that `oxylabs_tools()` returns `OxylabsTools` when the optional dependency is installed.
