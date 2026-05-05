## Summary

The primary symbol `neo4j_tools` is a lazy Agno toolkit class loader.
That exact loader behavior is repeated across many `src/mindroom/tools/*` modules, but it is a deliberate registry pattern and not a meaningful standalone refactor target.
The closest functional overlap is with database tool registrations in `sql.py`, `postgres.py`, `redshift.py`, and `google_bigquery.py`, which repeat credential fields and query/schema capability flags with provider-specific names.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
neo4j_tools	function	lines 95-99	related-only	neo4j_tools Neo4jTools agno.tools.neo4j database toolkit loaders ConfigField user password database enable_get_schema enable_run_cypher	sql.py:13-123; postgres.py:13-89; redshift.py:13-149; google_bigquery.py:13-93; duckdb.py:13-82; openbb.py:15-26,116-118
```

## Findings

### Related duplication: database toolkit metadata and lazy class loaders

- `src/mindroom/tools/neo4j.py:13-99` registers Neo4j metadata and returns `agno.tools.neo4j.Neo4jTools`.
- `src/mindroom/tools/sql.py:13-123` registers SQL metadata and returns `agno.tools.sql.SQLTools`.
- `src/mindroom/tools/postgres.py:13-89` registers PostgreSQL metadata and returns `agno.tools.postgres.PostgresTools`.
- `src/mindroom/tools/redshift.py:13-149` registers Redshift metadata and returns `agno.tools.redshift.RedshiftTools`.
- `src/mindroom/tools/google_bigquery.py:13-93` registers BigQuery metadata and returns `agno.tools.google.bigquery.GoogleBigQueryTools`.
- `src/mindroom/tools/duckdb.py:13-82` registers DuckDB metadata and returns `agno.tools.duckdb.DuckDbTools`.

The shared behavior is tool registration for database/query providers: declare connection/config fields, expose schema/table/query capability controls, declare dependencies/docs/function names, then lazily import and return an Agno toolkit class.
The duplication is related rather than directly duplicate because each provider has different constructor field names and capability names.
Neo4j uses `uri`, `user`, `password`, `database`, `enable_list_labels`, `enable_list_relationships`, `enable_get_schema`, and `enable_run_cypher`.
SQL uses `db_url`, `db_engine`, `schema`, `dialect`, `tables`, and SQL-specific enable flags.
Postgres and Redshift use host/port/database/user/password style fields, but preserve different defaults, IAM support, and function names.
BigQuery uses project/dataset/location fields and does not share credential semantics with Neo4j.

### Repeated but intentional: lazy Agno toolkit class returners

`neo4j_tools` itself has the same three-line shape as many other wrappers, including `sql_tools` at `src/mindroom/tools/sql.py:119-123`, `postgres_tools` at `src/mindroom/tools/postgres.py:85-89`, and `duckdb_tools` at `src/mindroom/tools/duckdb.py:78-82`.
This is mechanically duplicated, but the functions are also the public registry entry points imported from `src/mindroom/tools/__init__.py`.
Replacing them with a generic loader would likely add indirection without reducing behavior risk in this file.

## Proposed Generalization

No refactor recommended for `src/mindroom/tools/neo4j.py` alone.

If database tool metadata duplication becomes a broader cleanup target, the smallest useful helper would be a private metadata helper near `src/mindroom/tools/` for common `ConfigField` factories such as username, password, database, host, port, and `all`.
Provider-specific capability fields and constructor argument names should remain explicit in each module.

## Risk/tests

No production code was edited.
Any future refactor of shared config-field factories should verify exported tool metadata and saved tool configuration compatibility for `neo4j`, `sql`, `postgres`, `redshift`, `duckdb`, and `google_bigquery`.
Useful tests would compare `export_tools_metadata()` output before and after the refactor and instantiate configured toolkits with representative saved configs.
