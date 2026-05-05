## Summary

The top duplication candidate is the lazy Agno toolkit class loader pattern used by `custom_api_tools` and many sibling modules in `src/mindroom/tools`.
The wrapper behavior is functionally duplicated, but the duplication is low risk and currently buys straightforward per-tool typing, imports, and metadata registration.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
custom_api_tools	function	lines 91-95	duplicate-found	custom_api_tools; def *_tools() -> type; from agno.tools; return Toolkit class	src/mindroom/tools/serpapi.py:56; src/mindroom/tools/searxng.py:56; src/mindroom/tools/financial_datasets_api.py:50; src/mindroom/tools/website.py:35; src/mindroom/tools/calculator.py:27
```

## Findings

1. Lazy Agno toolkit class wrappers are repeated across tool modules.

`src/mindroom/tools/custom_api.py:91` defines `custom_api_tools`, imports `CustomApiTools` inside the function, and returns that class.
The same behavior appears in many sibling modules, including `src/mindroom/tools/serpapi.py:56`, `src/mindroom/tools/searxng.py:56`, `src/mindroom/tools/financial_datasets_api.py:50`, `src/mindroom/tools/website.py:35`, and `src/mindroom/tools/calculator.py:27`.
Each function defers importing the Agno toolkit until the registered tool factory is called, then returns the imported toolkit class unchanged.
The only meaningful differences are the imported module path, class name, return annotation, docstring, and surrounding `register_tool_with_metadata` arguments.

## Proposed Generalization

No immediate refactor recommended for this specific file.

If this pattern becomes painful to maintain, the minimal generalization would be a tiny helper in `src/mindroom/tools/_lazy_import.py`, such as a typed lazy class importer that takes a module path and class name.
Each tool module would still keep its own metadata decorator, but the function body could delegate to that helper.
That refactor should preserve per-tool function names because `src/mindroom/tools/__init__.py` exports those names directly.

## Risk/Tests

Risk is mainly around weakening static type information or changing import timing.
Tests should cover tool metadata registration and resolving representative tools with optional dependencies installed and missing.
No production code was edited.
