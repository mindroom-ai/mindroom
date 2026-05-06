## Summary

The only meaningful duplication candidate for `src/mindroom/tools/postgres.py` is the repeated lazy Agno toolkit factory pattern used by many `src/mindroom/tools/*.py` modules.
For `postgres_tools`, this is related structural duplication rather than a high-value refactor target because the unique registration metadata, function name, return type, and local import keep each tool explicit and cheap to maintain.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
postgres_tools	function	lines 85-89	related-only	postgres_tools; from agno.tools.postgres import PostgresTools; return PostgresTools; def *_tools() -> type[*Tools]; database tool wrappers	src/mindroom/tools/sql.py:119-123; src/mindroom/tools/duckdb.py:78-82; src/mindroom/tools/redshift.py:145-149; src/mindroom/tools/google_bigquery.py:89-93; src/mindroom/tools/calculator.py:27-31; src/mindroom/tools/arxiv.py:56-60; src/mindroom/tools/__init__.py:263-330
```

## Findings

### Related duplication: lazy toolkit-class factories

- `src/mindroom/tools/postgres.py:85-89` defines `postgres_tools`, imports `PostgresTools` inside the function, and returns the toolkit class.
- `src/mindroom/tools/sql.py:119-123`, `src/mindroom/tools/duckdb.py:78-82`, `src/mindroom/tools/redshift.py:145-149`, and `src/mindroom/tools/google_bigquery.py:89-93` use the same shape for database-related Agno toolkits.
- Broader examples include `src/mindroom/tools/calculator.py:27-31` and `src/mindroom/tools/arxiv.py:56-60`.
- The behavior is nearly the same: each decorated function is a registry entry point that defers importing an optional Agno toolkit dependency until the tool is requested, then returns the toolkit class.

Differences to preserve:

- Each wrapper has unique `@register_tool_with_metadata(...)` values, including name, UI labels, config fields, dependencies, docs URL, function names, status, and category.
- Each function has a typed return annotation for the concrete toolkit class.
- The local import is intentional because many tool dependencies are optional and should not be imported during registry import.
- `src/mindroom/tools/__init__.py:263-330` shows similar local-return wrappers for built-in or custom toolkits, but those are not direct duplicates of the Postgres Agno wrapper because the returned classes and registration semantics differ.

## Proposed Generalization

No refactor recommended for `postgres_tools` alone.

A possible mechanical helper such as `lazy_toolkit_factory("agno.tools.postgres", "PostgresTools")` would remove only two simple lines per module while making the decorated functions less explicit, weakening static typing, and still requiring per-tool functions for metadata registration and public `__all__` exports.
The current duplication is broad but intentionally shallow.

If the project later decides to generate these wrappers, it should be done as a larger registry/code-generation decision across most Agno-backed tools, not as a Postgres-local extraction.

## Risk/tests

No production code was changed, so no tests were run for this report-only audit.

If a future refactor centralizes lazy toolkit loading, tests should cover:

- metadata registration for a representative optional dependency-backed tool such as `postgres`;
- lazy import behavior when the optional dependency is missing;
- `TOOL_METADATA` and built-in registry entries after importing `mindroom.tools`;
- type-checking or runtime compatibility for consumers expecting the concrete factory functions exported by `src/mindroom/tools/__init__.py`.
