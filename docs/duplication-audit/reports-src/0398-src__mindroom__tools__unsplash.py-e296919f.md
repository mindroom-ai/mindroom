## Summary

Top duplication candidate: `unsplash_tools` is one instance of a repeated lazy-import toolkit factory pattern used throughout `src/mindroom/tools`.
The behavior is duplicated with many sibling modules such as `giphy_tools`, `dalle_tools`, `replicate_tools`, and `serpapi_tools`.
No production refactor is recommended from this file alone because the duplication is small, explicit, and coupled to decorator-based tool registration.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
unsplash_tools	function	lines 72-76	duplicate-found	"def *_tools() -> type", "from agno.tools.* import *Tools", "return *Tools", "UnsplashTools", "unsplash_tools"	src/mindroom/tools/giphy.py:56-60; src/mindroom/tools/dalle.py:84-88; src/mindroom/tools/replicate.py:56-60; src/mindroom/tools/serpapi.py:56-60; src/mindroom/tools/__init__.py:127,247
```

## Findings

### Repeated lazy-import toolkit factories

- `src/mindroom/tools/unsplash.py:72-76` defines `unsplash_tools`, imports `UnsplashTools` inside the function, and returns the toolkit class.
- `src/mindroom/tools/giphy.py:56-60` defines the same behavior for `GiphyTools`.
- `src/mindroom/tools/dalle.py:84-88` defines the same behavior for `DalleTools`.
- `src/mindroom/tools/replicate.py:56-60` defines the same behavior for `ReplicateTools`.
- `src/mindroom/tools/serpapi.py:56-60` defines the same behavior for `SerpApiTools`.

These functions are functionally the same: each is a no-argument factory registered through `register_tool_with_metadata`, performs a deferred import of an Agno toolkit class, and returns that class unchanged.
The differences to preserve are the imported class, return type annotation, docstring, and the surrounding metadata decorator values.

## Proposed Generalization

No refactor recommended for this isolated module.

A possible minimal generalization would be a helper such as `make_lazy_tool_loader(import_path: str, class_name: str)` in `src/mindroom/tool_system/metadata.py` or a nearby focused module, but applying it would make the registered functions less explicit and would require touching many tool modules for little behavioral gain.
Because the current duplication is only four lines per tool and keeps optional Agno imports lazy and readable, leaving it as-is is safer.

## Risk/Tests

Risk if generalized: decorator registration may depend on stable function names, annotations, or module-level imports exposed by `src/mindroom/tools/__init__.py`.
Any refactor would need tests covering tool registry discovery, metadata generation, optional dependency behavior when Agno extras are absent, and direct imports from `src/mindroom/tools/__init__.py`.

No tests were run because this audit did not edit production code.
