Summary: `tavily_tools` duplicates the standard MindRoom tool registration factory shape used by many Agno-backed tools: a metadata-decorated zero-argument function lazily imports and returns one toolkit class.
The duplication is real but intentionally small, and no refactor is recommended for this file alone because the surrounding metadata differs per toolkit and the factory body is only three lines.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
tavily_tools	function	lines 133-137	duplicate-found	tavily_tools; "Return Tavily tools"; "from agno.tools.* import *Tools"; "return *Tools"; "def .*_tools() -> type"	src/mindroom/tools/serpapi.py:56-60; src/mindroom/tools/linkup.py:63-67; src/mindroom/tools/exa.py:196-200; src/mindroom/tools/duckduckgo.py:98-102; src/mindroom/tools/csv.py:91-93
```

## Findings

### Agno toolkit factory wrapper duplication

- Primary behavior: `src/mindroom/tools/tavily.py:133-137` defines `tavily_tools`, lazily imports `TavilyTools` inside the factory, and returns the toolkit class.
- Matching behavior appears in `src/mindroom/tools/serpapi.py:56-60`, `src/mindroom/tools/linkup.py:63-67`, `src/mindroom/tools/exa.py:196-200`, `src/mindroom/tools/duckduckgo.py:98-102`, and `src/mindroom/tools/csv.py:91-93`.
- These factories are functionally the same: each preserves optional dependency import behavior by avoiding a runtime top-level import, then gives `register_tool_with_metadata` a callable that resolves the Agno toolkit class.
- Differences to preserve are the specific toolkit import path, returned class, type annotation, docstring, and per-tool decorator metadata such as config fields, dependencies, docs URL, and function names.

## Proposed generalization

No refactor recommended for this task.
If the project later chooses to deduplicate this pattern broadly, the smallest generalization would be a helper in `src/mindroom/tool_system/metadata.py` or a nearby focused module that builds a lazy toolkit-class factory from an import path and class name.
That helper would need to preserve the current factory callable shape expected by `register_tool_with_metadata`, keep optional dependencies lazy, and still allow clear per-tool type annotations or generated metadata.

## Risk/tests

- Main behavior risk: moving these lazy imports to a generic helper could accidentally import optional Agno integrations at module import time, breaking tools whose dependencies are not installed.
- Secondary risk: generated or generic factories may make metadata validation and function ownership less transparent.
- Tests would need to cover tool registry import with missing optional dependencies, metadata registration for at least Tavily and another search toolkit, and `get_tool_by_name` or equivalent instantiation for configured toolkit kwargs.
