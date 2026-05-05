# Summary

The only behavior in `src/mindroom/tools/arxiv.py` is the lazy Agno toolkit-class factory `arxiv_tools`.
That behavior is duplicated across many tool wrapper modules: each function imports one toolkit class inside the factory and returns the class object for registration/instantiation elsewhere.
No ArXiv-specific search or paper-reading implementation is duplicated in MindRoom source; those behaviors are delegated to `agno.tools.arxiv.ArxivTools`.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
arxiv_tools	function	lines 56-60	duplicate-found	"def .*_tools", "from agno.tools.arxiv import ArxivTools", "return ArxivTools", lazy toolkit factories	src/mindroom/tools/pubmed.py:63, src/mindroom/tools/wikipedia.py:42, src/mindroom/tools/hackernews.py:49, src/mindroom/tools/__init__.py:272, src/mindroom/tools/__init__.py:326
```

# Findings

## Lazy toolkit-class factory pattern is repeated

- `src/mindroom/tools/arxiv.py:56` defines `arxiv_tools()`, imports `ArxivTools` inside the function, and returns the toolkit class at `src/mindroom/tools/arxiv.py:58-60`.
- `src/mindroom/tools/pubmed.py:63` does the same for `PubmedTools`, importing inside the function and returning the class at `src/mindroom/tools/pubmed.py:65-67`.
- `src/mindroom/tools/wikipedia.py:42` does the same for `WikipediaTools`, importing inside the function and returning the class at `src/mindroom/tools/wikipedia.py:44-46`.
- `src/mindroom/tools/hackernews.py:49` does the same for `HackerNewsTools`, importing inside the function and returning the class at `src/mindroom/tools/hackernews.py:51-53`.
- `src/mindroom/tools/__init__.py:272` and `src/mindroom/tools/__init__.py:326` show the same local-import-and-return-class factory shape for special bundled/custom toolkit registrations.

These are functionally the same behavior: defer importing an optional toolkit dependency until the registered factory is used, then return the toolkit class object expected by the tool registry.
The differences to preserve are the concrete import path, return type, docstring, metadata decorator arguments, dependencies, and function names.

This is a real duplication pattern, but it is intentionally small and data-adjacent.
The wrapper modules are mostly declarative metadata; introducing an abstraction would only remove three executable lines per wrapper while making registration less direct.

# Proposed Generalization

No refactor recommended for `arxiv_tools` in isolation.

If the project later decides to consolidate many generated/simple tool wrappers at once, the minimal generalization would be a small helper in `src/mindroom/tool_system/metadata.py` or a dedicated generator used by tooling, not a broader runtime abstraction.
That helper would need to accept the fully qualified toolkit import path and return a typed lazy factory, while preserving the current `register_tool_with_metadata` decorator contract.

# Risk/Tests

Changing this pattern could affect optional dependency behavior because imports are intentionally delayed until a tool is actually loaded.
Tests should cover metadata registration and instantiation for at least one no-config toolkit with optional dependencies, including the failure mode when the optional package is absent.

No production code was edited for this audit.
