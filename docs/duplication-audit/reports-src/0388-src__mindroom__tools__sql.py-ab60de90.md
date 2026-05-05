Summary: `sql_tools` duplicates the common registered-tool factory pattern used across many `src/mindroom/tools/*` modules: a `TYPE_CHECKING` import, a metadata decorator, a lazy runtime import, and returning the Agno toolkit class.
This is real duplication, but it is intentionally tiny and keeps optional Agno/tool dependencies lazy, so no refactor is recommended for this file alone.
Database-adjacent tools expose related table-description/query operations, but their local behavior is metadata declaration plus toolkit-class return, not duplicated SQL execution logic in MindRoom source.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
sql_tools	function	lines 119-123	duplicate-found	"def *_tools() -> type", "from agno.tools.sql import SQLTools", "return SQLTools", database toolkit factories	src/mindroom/tools/duckdb.py:78-82; src/mindroom/tools/pandas.py:49-53; src/mindroom/tools/csv.py:91-95; src/mindroom/tools/postgres.py:85-89; src/mindroom/tools/redshift.py:145-149; src/mindroom/tools/google_bigquery.py:89-93; src/mindroom/tool_system/metadata.py:554-564
```

Findings:

1. Repeated lazy toolkit-class factory pattern.
   `src/mindroom/tools/sql.py:119-123` imports `SQLTools` inside `sql_tools()` and returns the class.
   The same behavior appears in `src/mindroom/tools/duckdb.py:78-82`, `src/mindroom/tools/pandas.py:49-53`, `src/mindroom/tools/csv.py:91-95`, `src/mindroom/tools/postgres.py:85-89`, `src/mindroom/tools/redshift.py:145-149`, and `src/mindroom/tools/google_bigquery.py:89-93`.
   These functions all serve the same registry-facing purpose: keep optional dependencies out of module import time while giving `register_tool_with_metadata` a callable that resolves to an Agno toolkit class.
   The runtime construction path in `src/mindroom/tool_system/metadata.py:554-564` expects the returned class and instantiates/wraps it consistently.
   Differences to preserve are the concrete imported class, return type annotation, docstring, and each module's metadata fields.

2. Related database operation exposure without duplicated local execution logic.
   SQL, DuckDB, PostgreSQL, Redshift, CSV, and BigQuery metadata all expose variations of listing tables, describing tables, and running queries.
   For example, `src/mindroom/tools/sql.py:117` lists `describe_table`, `list_tables`, `run_sql`, and `run_sql_query`; `src/mindroom/tools/postgres.py:76-83` and `src/mindroom/tools/redshift.py:136-143` list query/table functions; `src/mindroom/tools/google_bigquery.py:87` lists `describe_table`, `list_tables`, and `run_sql_query`.
   This is related behavior, but MindRoom is not implementing the same SQL query execution multiple times here.
   The local modules are thin metadata adapters over different upstream Agno toolkits, with provider-specific config fields and dependencies.

Proposed generalization:

No refactor recommended for `sql_tools` alone.
If this pattern is generalized later across the whole tools directory, the minimal helper would be a typed lazy-class resolver in `src/mindroom/tool_system/metadata.py` or a small local helper module, parameterized by import path and class name.
That refactor would need to preserve lazy imports, static type-checking imports, decorator registration timing, readable per-tool modules, and per-tool metadata differences.

Risk/tests:

The main risk in deduplicating this factory shape is accidentally importing optional tool dependencies at module import time or weakening type checking/readability for each registered tool.
Tests should cover importing `mindroom.tools` without optional database dependencies installed, resolving `sql` through the tool registry, and instantiating the returned toolkit through the existing metadata path with SQL config overrides.
No production code was edited.
