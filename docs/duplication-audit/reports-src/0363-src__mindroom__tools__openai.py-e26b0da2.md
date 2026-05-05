## Summary

No meaningful duplication found for `openai_tools` beyond the repository-wide tool registry pattern.
The function duplicates the same lazy-import-and-return behavior used by many `src/mindroom/tools/*` modules, but that behavior is intentionally tiny and tied to each module's metadata decorator.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
openai_tools	function	lines 119-123	related-only	openai_tools OpenAITools agno.tools.openai def *_tools register_tool_with_metadata lazy import return Toolkit	src/mindroom/tools/gemini.py:91; src/mindroom/tools/dalle.py:84; src/mindroom/tools/eleven_labs.py:91; src/mindroom/tools/cartesia.py:77; src/mindroom/tools/__init__.py:263
```

## Findings

No real duplication requiring refactor.

`src/mindroom/tools/openai.py:119` uses the standard registered toolkit factory shape: import the Agno toolkit class inside the function and return the class.
The same behavior appears in provider modules such as `src/mindroom/tools/gemini.py:91`, `src/mindroom/tools/dalle.py:84`, `src/mindroom/tools/eleven_labs.py:91`, and `src/mindroom/tools/cartesia.py:77`.
These functions are functionally equivalent as lazy class factories, but each is coupled to a distinct decorator payload, type-only import, docstring, dependency list, docs URL, and exported symbol name.

This is related duplication rather than an active maintenance problem.
The duplicated body is two executable lines, and extracting it would likely make the registration modules harder to read because the import target would need to become data or a string.

## Proposed Generalization

No refactor recommended.

If this pattern grows more complex later, a small helper in `src/mindroom/tool_system/metadata.py` or a nearby registry utility could build lazy toolkit factories from import strings.
That is not justified for the current `openai_tools` behavior because it would trade two local lines for indirection across every tool module.

## Risk/tests

Changing this pattern would risk tool registry import behavior and optional dependency isolation.
If a future refactor extracts lazy toolkit factories, tests should cover registry loading, missing optional dependencies, and at least one dynamic toolkit load path for an API-key-backed Agno toolkit.
