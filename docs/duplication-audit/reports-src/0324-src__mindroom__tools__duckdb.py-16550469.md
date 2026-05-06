## Summary

Top duplication candidate: `duckdb_tools` repeats the same lazy Agno toolkit factory pattern used across many `src/mindroom/tools/*` modules.
The closest behavior-level matches are the database-adjacent factories in `csv.py`, `sql.py`, and `postgres.py`.
No production refactor is recommended from this single file alone because the repeated three-line factory keeps each tool module explicit and the current metadata decorator remains the real source of per-tool variation.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
duckdb_tools	function	lines 78-82	duplicate-found	duckdb_tools lazy import return Agno Tools; def *_tools() -> type[*Tools]; database tool factories	src/mindroom/tools/csv.py:91; src/mindroom/tools/sql.py:119; src/mindroom/tools/postgres.py:85; src/mindroom/tools/calculator.py:27; src/mindroom/tools/pandas.py:49
```

## Findings

### Repeated lazy Agno toolkit class factory

- Primary behavior: `src/mindroom/tools/duckdb.py:78` defines `duckdb_tools`, imports `DuckDbTools` inside the factory at `src/mindroom/tools/duckdb.py:80`, and returns that class at `src/mindroom/tools/duckdb.py:82`.
- Duplicate behavior appears in `src/mindroom/tools/csv.py:91`, which imports `CsvTools` inside `csv_tools` at `src/mindroom/tools/csv.py:93` and returns it at `src/mindroom/tools/csv.py:95`.
- Duplicate behavior appears in `src/mindroom/tools/sql.py:119`, which imports `SQLTools` inside `sql_tools` at `src/mindroom/tools/sql.py:121` and returns it at `src/mindroom/tools/sql.py:123`.
- Duplicate behavior appears in `src/mindroom/tools/postgres.py:85`, which imports `PostgresTools` inside `postgres_tools` at `src/mindroom/tools/postgres.py:87` and returns it at `src/mindroom/tools/postgres.py:89`.
- Broader examples include `src/mindroom/tools/calculator.py:27` and `src/mindroom/tools/pandas.py:49`, following the same `TYPE_CHECKING` import plus runtime local import plus class return pattern.

The duplicated behavior is the factory wrapper around an Agno toolkit class, not the toolkit functionality itself.
The important differences to preserve are the imported Agno class, the function name exported from `src/mindroom/tools/__init__.py`, the docstring, and the metadata attached by `register_tool_with_metadata`.

### Database tool metadata overlaps but does not duplicate behavior enough to refactor

- `src/mindroom/tools/duckdb.py:61` and `src/mindroom/tools/postgres.py:76` expose overlapping database operations such as `describe_table`, `export_table_to_path`, `inspect_query`, `run_query`, `show_tables`, and `summarize_table`.
- `src/mindroom/tools/sql.py:117` exposes related SQL operations such as `describe_table`, `list_tables`, and `run_sql_query`.
- `src/mindroom/tools/csv.py:87` depends on `duckdb` and exposes CSV querying behavior through DuckDB-backed Agno tools.

This is related capability overlap rather than duplicated MindRoom implementation.
Each module is registering a different upstream Agno toolkit with different constructor fields, dependencies, setup requirements, and docs URLs.

## Proposed Generalization

No refactor recommended for this task.

A possible future cleanup would be a tiny helper for simple Agno toolkit factories, but it would need to preserve lazy imports and typing without making `register_tool_with_metadata` harder to read.
Given this file has only one behavior symbol and the repeated implementation is intentionally explicit, the helper would likely reduce only three runtime lines per tool while adding indirection across many modules.

## Risk/Tests

If a future refactor centralizes these factories, risks include eager-importing optional Agno dependencies, breaking optional dependency isolation, changing the exported tool factory names used by tool registration/import code, or losing static type clarity from the `TYPE_CHECKING` imports.
Tests should cover tool registry import of `duckdb`, `csv`, `sql`, and `postgres` with missing optional dependencies where applicable, plus a positive path confirming each factory returns the expected Agno toolkit class when its dependency is installed.
