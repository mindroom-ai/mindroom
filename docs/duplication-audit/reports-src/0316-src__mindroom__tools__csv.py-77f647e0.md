## Summary

No meaningful duplication found for `src/mindroom/tools/csv.py`.
The module is a declarative registration wrapper around Agno's `CsvTools`, and searches did not find another CSV-specific parser, reader, column inspector, or query wrapper under `src`.
The lazy tool factory shape is repeated across many `src/mindroom/tools/*.py` modules, but that is an intentional registry convention and not a useful refactor target for this primary file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
csv_tools	function	lines 91-95	related-only	csv_tools CsvTools csv toolkit read_csv_file query_csv_file get_columns list_csv_files register_tool_with_metadata lazy tool factory	src/mindroom/tools/pandas.py:49; src/mindroom/tools/duckdb.py:78; src/mindroom/tools/sql.py:119; src/mindroom/tools_metadata.json:6960
```

## Findings

No real duplication found.

`csv_tools` at `src/mindroom/tools/csv.py:91` only lazily imports and returns `agno.tools.csv_toolkit.CsvTools`.
The closest related modules are `src/mindroom/tools/pandas.py:49`, `src/mindroom/tools/duckdb.py:78`, and `src/mindroom/tools/sql.py:119`, which use the same metadata-decorated lazy factory pattern for different Agno toolkit classes.
They overlap in data-analysis domain, but they expose different toolkits, different config fields, and different function names, so consolidating them would mostly abstract boilerplate without reducing duplicated CSV behavior.

`src/mindroom/tools_metadata.json:6960` mirrors the CSV registration metadata, including config fields and function names.
This appears to be generated registry data rather than an independent source implementation, so it is related only and should not drive a source refactor.

Searches for CSV-specific operations such as `read_csv`, `write_csv`, `csv.reader`, `csv.writer`, `DictReader`, `pd.read_csv`, and `to_csv` found no other source implementation under `src`.

## Proposed Generalization

No refactor recommended.

A generic helper for one-line Agno tool factories could reduce boilerplate across many tool registration modules, but it would add indirection to a declarative convention and is outside the scope of a CSV-specific duplication finding.

## Risk/Tests

No production change is recommended, so no behavior risk is introduced.
If the repeated tool-factory convention were ever refactored globally, tests should cover tool metadata registration, lazy import behavior for optional dependencies, and generated metadata consistency for at least CSV, DuckDB, SQL, and Pandas tools.
