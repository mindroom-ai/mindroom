## Summary

The primary file contains one metadata-registered Agno toolkit loader.
The loader behavior is repeated across many sibling modules in `src/mindroom/tools`: each wrapper exposes metadata, keeps the Agno import under `TYPE_CHECKING` plus a lazy runtime import, and returns the toolkit class.
This is real repeated registry boilerplate, but it is not calculator-specific business logic and the metadata payloads differ per tool, so no refactor is recommended from this module alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
calculator_tools	function	lines 27-31	related-only	calculator_tools, CalculatorTools, agno.tools.calculator, def *_tools returns Agno toolkit	src/mindroom/tools/calculator.py:27; src/mindroom/tools/wikipedia.py:42; src/mindroom/tools/sleep.py:42; src/mindroom/tools/website.py:35; src/mindroom/tools/hackernews.py:49; src/mindroom/tools/__init__.py:40
```

## Findings

No calculator-specific duplicated behavior was found elsewhere under `./src`.

Related repeated behavior exists in Agno toolkit wrapper modules:

- `src/mindroom/tools/calculator.py:27` defines `calculator_tools()`, lazily imports `CalculatorTools`, and returns the class.
- `src/mindroom/tools/wikipedia.py:42` defines `wikipedia_tools()`, lazily imports `WikipediaTools`, and returns the class.
- `src/mindroom/tools/sleep.py:42` defines `sleep_tools()`, lazily imports `SleepTools`, and returns the class.
- `src/mindroom/tools/website.py:35` defines `website_tools()`, lazily imports `WebsiteTools`, and returns the class.
- `src/mindroom/tools/hackernews.py:49` defines `hackernews_tools()`, lazily imports `HackerNewsTools`, and returns the class.

These are functionally similar because each module uses `register_tool_with_metadata` to register a tool and provides a zero-argument function that returns an Agno toolkit class.
The differences to preserve are the metadata fields, imported Agno class, dependency list, docs URL, and function names.

## Proposed Generalization

No refactor recommended from this single primary file.

A possible future cleanup, if many simple Agno wrappers are being edited together, would be a small helper in `mindroom.tool_system.metadata` or a new focused module such as `mindroom.tool_system.agno_loader` that builds these lazy class-returning functions from an import path.
That helper would need to preserve static typing, registration metadata readability, and the current lazy import behavior.
Given the current file is only 31 lines and the duplication is mostly declarative metadata, introducing indirection may reduce clarity more than it reduces maintenance cost.

## Risk/tests

No production code was changed.
If the related wrapper pattern is ever consolidated, tests should cover tool registration discovery, metadata visibility for `calculator`, and runtime loading of the returned `CalculatorTools` class.
The main behavior risk would be changing import timing or breaking `src/mindroom/tools/__init__.py` exports.
