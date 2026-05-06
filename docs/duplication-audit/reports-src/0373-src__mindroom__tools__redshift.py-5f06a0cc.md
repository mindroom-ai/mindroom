Summary: `redshift_tools` duplicates the standard lazy toolkit factory shape used across many `src/mindroom/tools/*` registration modules, and its closest behavioral overlap is with PostgreSQL/SQL database toolkit registrations. No meaningful refactor is recommended because the duplicate body is intentionally tiny and each factory carries distinct decorator metadata.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
redshift_tools	function	lines 145-149	related-only	redshift_tools; RedshiftTools; def .*_tools; database toolkit factories; describe_table/run_query/show_tables	src/mindroom/tools/redshift.py:145; src/mindroom/tools/postgres.py:85; src/mindroom/tools/sql.py:119; src/mindroom/tools/duckdb.py:78; src/mindroom/tools/csv.py:91
```

## Findings

No production-code duplication worth extracting was found for `redshift_tools`.

The function body at `src/mindroom/tools/redshift.py:145` is the same lazy factory pattern used by nearby toolkit registration modules: import the Agno toolkit inside the factory and return the toolkit class.
Examples include `src/mindroom/tools/postgres.py:85`, `src/mindroom/tools/sql.py:119`, `src/mindroom/tools/duckdb.py:78`, and `src/mindroom/tools/csv.py:91`.
This is functionally related behavior, but the duplicated code is only the per-tool registry adapter required by `register_tool_with_metadata`.

`src/mindroom/tools/postgres.py:13` is the closest metadata overlap.
PostgreSQL and Redshift both register configured SQL-like database toolkits with host, port, user, password, table schema, dependencies, docs URL, and the same core function names: `describe_table`, `export_table_to_path`, `inspect_query`, `run_query`, `show_tables`, and `summarize_table`.
The behavior is related because both expose Agno database operations through MindRoom metadata, but the details differ enough to keep separate: Redshift uses port `5439`, `database`, IAM/AWS credential fields, Redshift icon/docs/dependency, and optional cluster/region fields; PostgreSQL uses port `5432`, `db_name`, a psycopg connection override, and PostgreSQL-specific metadata.

`src/mindroom/tools/sql.py:13` and `src/mindroom/tools/duckdb.py:13` are broader database toolkit registrations with overlapping query/list/describe functionality.
They do not duplicate Redshift-specific configuration behavior.
Their config schemas, dependencies, function names, category/status, and setup expectations are different.

## Proposed Generalization

No refactor recommended.

A generic "return this imported toolkit class" helper would add indirection without reducing meaningful behavior.
A shared database metadata helper for common fields or SQL function-name tuples could reduce a few lines, but it would parameterize nearly every interesting difference and make these simple registry modules harder to scan.

## Risk/Tests

No code changes were made.

If a future refactor did extract shared database metadata constants, tests should cover metadata export for `redshift`, `postgres`, `sql`, and `duckdb`, including required config fields, dependency lists, docs URLs, and function-name snapshots.
