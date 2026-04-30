# Data & Databases

Use these tools to query SQL and graph databases, analyze tabular files, work with Google datasets and spreadsheets, and fetch financial or business data.

## What This Page Covers

This page documents the built-in tools in the `data-and-databases` group.
Use these tools when you need database access, dataframe-style analysis, spreadsheet automation, or market and company data.

## Tools On This Page

- \[`sql`\] - Generic SQLAlchemy-backed SQL access for databases that can be reached by URL or engine.
- \[`postgres`\] - PostgreSQL-specific table inspection, query analysis, querying, and export.
- \[`redshift`\] - Amazon Redshift warehouse access with password or IAM-based authentication.
- \[`neo4j`\] - Neo4j graph inspection and Cypher queries.
- \[`duckdb`\] - Local analytical SQL with file loading, exports, and full-text helpers.
- \[`csv`\] - Pre-registered CSV reading and DuckDB-backed SQL queries over CSV files.
- \[`pandas`\] - In-memory dataframe creation and dataframe method execution.
- \[`google_bigquery`\] - BigQuery dataset inspection and SQL queries.
- \[`google_sheets`\] - Google Sheets access through Google OAuth, with read support verified by default and upstream support for create, update, and duplicate operations.
- \[`openbb`\] - Stock prices, company search, news, profiles, and price targets through OpenBB.
- \[`yfinance`\] - Yahoo Finance market data, fundamentals, news, and history.
- \[`financial_datasets_api`\] - Structured financial statements, filings, ownership, and crypto data from Financial Datasets.

## Common Setup Notes

`sql`, `postgres`, `redshift`, `neo4j`, `google_bigquery`, `google_sheets`, and `financial_datasets_api` are registered as `requires_config`, so they stay unavailable in the dashboard until their required config or auth is present.
`duckdb`, `csv`, `pandas`, `openbb`, and `yfinance` are `setup_type: none`, so they can be enabled immediately once their optional Python dependencies are installed.
MindRoom validates inline tool overrides against the declared `config_fields`, and `type="password"` fields such as `password`, `secret_access_key`, and `api_key` must go through the dashboard or credential store instead of inline YAML.
Several fields on this page are advanced constructor inputs rather than normal `config.yaml` values, including `db_engine`, `connection`, `credentials`, `duckdb_connection`, `duckdb_kwargs`, `obb`, and `session`.
Token-like fields such as `openbb_pat` are better kept in stored credentials even when the current metadata does not mark them as password fields.
`src/mindroom/api/integrations.py` currently contains Spotify-specific OAuth endpoints only, while Google Sheets uses the generic `/api/oauth/google_sheets/*` flow.
`google_sheets` declares `auth_provider="google_sheets"` and stores OAuth tokens separately from editable Sheets tool settings.
`csv` queries use DuckDB under the hood, and `duckdb` is the better fit when you need to create tables from files, export results, or load local and S3 data repeatedly.
Missing optional dependencies can auto-install at first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.

## \[`sql`\]

`sql` is the generic SQL toolkit for database engines that SQLAlchemy can open directly.

### What It Does

`sql` exposes `list_tables()`, `describe_table()`, and `run_sql_query()`.
The toolkit can connect through `db_url`, an existing `db_engine`, or a URL assembled from `user`, `password`, `host`, `port`, `schema`, and `dialect`.
`list_tables()` and `describe_table()` use SQLAlchemy inspection, and `run_sql_query()` returns JSON rows with a default limit of 10 unless you pass `limit=None`.
If you pass a `tables` mapping, `list_tables()` returns that mapping directly instead of live database introspection.
For dialects where database name and schema are distinct concepts, `db_url` is the safest authored configuration because the generic `schema` field is used both in the constructed URL path and in later table inspection calls.

### Configuration

| Option                  | Type       | Required | Default | Notes                                                                      |
| ----------------------- | ---------- | -------- | ------- | -------------------------------------------------------------------------- |
| `db_url`                | `url`      | `no`     | `null`  | Preferred authored connection string.                                      |
| `db_engine`             | `text`     | `no`     | `null`  | Advanced programmatic SQLAlchemy `Engine` input, not the normal YAML path. |
| `user`                  | `text`     | `no`     | `null`  | Username for URL assembly when not using `db_url`.                         |
| `password`              | `password` | `no`     | `null`  | Database password stored through the dashboard or credential store.        |
| `host`                  | `url`      | `no`     | `null`  | Database host for URL assembly.                                            |
| `port`                  | `number`   | `no`     | `null`  | Database port for URL assembly.                                            |
| `schema`                | `text`     | `no`     | `null`  | Schema name, and also the path segment in the assembled generic URL.       |
| `dialect`               | `text`     | `no`     | `null`  | SQLAlchemy dialect prefix such as `postgresql`, `mysql`, or `sqlite`.      |
| `tables`                | `text`     | `no`     | `null`  | Advanced predeclared table metadata mapping.                               |
| `enable_list_tables`    | `boolean`  | `no`     | `true`  | Enable `list_tables()`.                                                    |
| `enable_describe_table` | `boolean`  | `no`     | `true`  | Enable `describe_table()`.                                                 |
| `enable_run_sql_query`  | `boolean`  | `no`     | `true`  | Enable `run_sql_query()`.                                                  |
| `all`                   | `boolean`  | `no`     | `false` | Enable the full upstream SQL tool surface.                                 |

### Example

```
agents:
  analyst:
    tools:
      - sql:
          db_url: sqlite:////tmp/analytics.db
          enable_run_sql_query: true
```

```
list_tables()
describe_table("events")
run_sql_query("SELECT * FROM events ORDER BY created_at DESC", limit=20)
```

### Notes

- Use `db_url` for normal YAML authoring, because `db_engine` expects a live SQLAlchemy object rather than a string.
- If you need passwords, store them through the dashboard or credential store instead of inline YAML.
- This generic toolkit is useful for simple SQL inspection, but `postgres` and `redshift` expose richer warehouse-style helpers such as query inspection and exports.

## \[`postgres`\]

`postgres` is the PostgreSQL-specific toolkit for read-only schema inspection, query review, querying, and CSV export.

### What It Does

`postgres` exposes `show_tables()`, `describe_table()`, `summarize_table()`, `inspect_query()`, `run_query()`, and `export_table_to_path()`.
The toolkit opens a Psycopg connection, sets `search_path` to `table_schema`, and marks the connection read-only.
`inspect_query()` runs `EXPLAIN`, which makes it the safe first step before a larger `run_query()`.
`export_table_to_path()` writes queryable table output to a local file path from the running process.

### Configuration

| Option         | Type       | Required | Default  | Notes                                                                                             |
| -------------- | ---------- | -------- | -------- | ------------------------------------------------------------------------------------------------- |
| `connection`   | `text`     | `no`     | `null`   | Programmatic existing Psycopg connection object, not usable from the UI or normal YAML authoring. |
| `host`         | `url`      | `yes`    | `null`   | PostgreSQL server hostname.                                                                       |
| `port`         | `number`   | `no`     | `5432`   | PostgreSQL server port.                                                                           |
| `db_name`      | `text`     | `yes`    | `null`   | Database name.                                                                                    |
| `user`         | `text`     | `yes`    | `null`   | PostgreSQL username.                                                                              |
| `password`     | `password` | `yes`    | `null`   | PostgreSQL password stored through the dashboard or credential store.                             |
| `table_schema` | `text`     | `no`     | `public` | Schema used for table operations and connection `search_path`.                                    |

### Example

```
agents:
  warehouse:
    tools:
      - postgres:
          host: warehouse.internal
          port: 5432
          db_name: analytics
          user: analyst
          table_schema: reporting
```

```
show_tables()
describe_table("daily_revenue")
inspect_query("SELECT * FROM daily_revenue WHERE day >= CURRENT_DATE - INTERVAL '7 days'")
run_query("SELECT day, total FROM daily_revenue ORDER BY day DESC LIMIT 7")
export_table_to_path("daily_revenue", "/tmp/daily_revenue.csv")
```

### Notes

- `postgres` is read-only by design in the upstream toolkit, which makes it a safer default than generic unrestricted SQL access.
- Use `connection` only for programmatic instantiation where you already have a Psycopg connection object.
- Secrets such as `password` must be stored outside authored YAML.

## \[`redshift`\]

`redshift` is the warehouse-oriented toolkit for Amazon Redshift clusters with either password or IAM authentication.

### What It Does

`redshift` exposes `show_tables()`, `describe_table()`, `summarize_table()`, `inspect_query()`, `run_query()`, and `export_table_to_path()`.
The upstream connector supports standard `user` and `password` authentication, IAM authentication through `profile`, and IAM authentication through explicit AWS credentials.
When IAM auth is enabled, the toolkit can fall back to environment variables such as `REDSHIFT_HOST`, `REDSHIFT_DATABASE`, `REDSHIFT_CLUSTER_IDENTIFIER`, `AWS_REGION`, and `AWS_PROFILE`.
`table_schema` defaults to `public`, and `ssl` defaults to `true`.

### Configuration

| Option               | Type       | Required | Default  | Notes                                                                   |
| -------------------- | ---------- | -------- | -------- | ----------------------------------------------------------------------- |
| `host`               | `url`      | `yes`    | `null`   | Redshift cluster endpoint.                                              |
| `port`               | `number`   | `no`     | `5439`   | Redshift port.                                                          |
| `database`           | `text`     | `yes`    | `null`   | Database name.                                                          |
| `user`               | `text`     | `yes`    | `null`   | Username for password auth.                                             |
| `password`           | `password` | `yes`    | `null`   | Password for standard authentication.                                   |
| `iam`                | `boolean`  | `no`     | `false`  | Use IAM-based auth instead of password auth.                            |
| `cluster_identifier` | `text`     | `no`     | `null`   | Cluster identifier, required for IAM auth against provisioned clusters. |
| `region`             | `text`     | `no`     | `null`   | AWS region for IAM auth.                                                |
| `db_user`            | `text`     | `no`     | `null`   | Database user for IAM auth.                                             |
| `access_key_id`      | `password` | `no`     | `null`   | Optional AWS access key for IAM auth.                                   |
| `secret_access_key`  | `password` | `no`     | `null`   | Optional AWS secret key for IAM auth.                                   |
| `session_token`      | `password` | `no`     | `null`   | Optional AWS session token for temporary credentials.                   |
| `profile`            | `text`     | `no`     | `null`   | AWS profile name for IAM auth.                                          |
| `ssl`                | `boolean`  | `no`     | `true`   | Enable SSL.                                                             |
| `table_schema`       | `text`     | `no`     | `public` | Schema used for table operations.                                       |

### Example

```
agents:
  warehouse:
    tools:
      - redshift:
          host: my-cluster.abc123.us-east-1.redshift.amazonaws.com
          database: dev
          iam: true
          cluster_identifier: analytics-prod
          region: us-east-1
          db_user: analyst
```

```
show_tables()
describe_table("fact_orders")
inspect_query("SELECT COUNT(*) FROM fact_orders WHERE created_at >= current_date - 30")
run_query("SELECT created_at::date, COUNT(*) FROM fact_orders GROUP BY 1 ORDER BY 1 DESC")
export_table_to_path("fact_orders", "/tmp/fact_orders.csv")
```

### Notes

- If `iam: true`, the toolkit can use `profile` or explicit AWS credentials instead of `user` and `password`.
- If you use password auth, store `password` through the dashboard or credential store rather than inline YAML.
- `redshift` is the better fit than generic `sql` when you want Redshift-aware connection options and warehouse export helpers.

## \[`neo4j`\]

`neo4j` is the graph database toolkit for labels, relationship types, schema discovery, and Cypher queries.

### What It Does

`neo4j` exposes `list_labels()`, `list_relationship_types()`, `get_schema()`, and `run_cypher_query()`.
It uses the Neo4j Python driver and can target a specific database when `database` is set.
The individual enable flags let you expose schema discovery without allowing arbitrary Cypher execution.

### Configuration

| Option                      | Type       | Required | Default | Notes                                                            |
| --------------------------- | ---------- | -------- | ------- | ---------------------------------------------------------------- |
| `uri`                       | `url`      | `no`     | `null`  | Neo4j connection URI such as `bolt://localhost:7687`.            |
| `user`                      | `text`     | `yes`    | `null`  | Neo4j username.                                                  |
| `password`                  | `password` | `yes`    | `null`  | Neo4j password stored through the dashboard or credential store. |
| `database`                  | `text`     | `no`     | `null`  | Optional target database name.                                   |
| `enable_list_labels`        | `boolean`  | `no`     | `true`  | Enable `list_labels()`.                                          |
| `enable_list_relationships` | `boolean`  | `no`     | `true`  | Enable `list_relationship_types()`.                              |
| `enable_get_schema`         | `boolean`  | `no`     | `true`  | Enable `get_schema()`.                                           |
| `enable_run_cypher`         | `boolean`  | `no`     | `true`  | Enable `run_cypher_query()`.                                     |
| `all`                       | `boolean`  | `no`     | `false` | Enable the full Neo4j toolkit.                                   |

### Example

```
agents:
  graph:
    tools:
      - neo4j:
          uri: bolt://graph.internal:7687
          user: neo4j
          database: analytics
          enable_run_cypher: true
```

```
list_labels()
list_relationship_types()
get_schema()
run_cypher_query("MATCH (u:User)-[:PLACED]->(o:Order) RETURN u.id, count(o) AS orders LIMIT 10")
```

### Notes

- `uri` is optional in metadata, but in practice you should supply it unless the runtime injects a connection some other way.
- Store the Neo4j password through the dashboard or credential store instead of inline YAML.
- Disable `enable_run_cypher` when you want schema visibility without free-form graph queries.

## \[`duckdb`\]

`duckdb` is the local analytical SQL engine for file-backed analytics, table creation, exports, and full-text indexing.

### What It Does

`duckdb` exposes `show_tables()`, `describe_table()`, `inspect_query()`, `run_query()`, `summarize_table()`, `create_table_from_path()`, `export_table_to_path()`, `load_local_path_to_table()`, `load_local_csv_to_table()`, `load_s3_path_to_table()`, `load_s3_csv_to_table()`, `create_fts_index()`, and `full_text_search()`.
If `db_path` is unset, DuckDB runs in memory.
`create_table_from_path()` can load CSV or other file formats directly into a table, and `export_table_to_path()` defaults to `PARQUET`.
`init_commands`, `connection`, and `config` are advanced constructor inputs that are passed directly to the upstream toolkit.

### Configuration

| Option          | Type      | Required | Default | Notes                                                              |
| --------------- | --------- | -------- | ------- | ------------------------------------------------------------------ |
| `db_path`       | `text`    | `no`     | `null`  | Path to a persistent DuckDB database file.                         |
| `connection`    | `text`    | `no`     | `null`  | Advanced existing DuckDB connection object, not normal YAML input. |
| `init_commands` | `text`    | `no`     | `null`  | Advanced startup commands passed through to the toolkit.           |
| `read_only`     | `boolean` | `no`     | `false` | Open the database in read-only mode.                               |
| `config`        | `text`    | `no`     | `null`  | Advanced raw DuckDB config mapping.                                |

### Example

```
agents:
  analyst:
    tools:
      - duckdb:
          db_path: data/analytics.duckdb
          read_only: false
```

```
show_tables(True)
create_table_from_path("/workspace/data/orders.parquet", table="orders", replace=True)
inspect_query("SELECT customer_id, COUNT(*) FROM orders GROUP BY 1")
run_query("SELECT customer_id, COUNT(*) AS orders FROM orders GROUP BY 1 ORDER BY orders DESC LIMIT 10")
export_table_to_path("orders", format="CSV", path="/tmp")
```

### Notes

- Use `duckdb` when you need repeatable local analytics over files, especially Parquet, CSV, or S3-backed datasets.
- If you only need quick reads and SQL over a few predeclared CSVs, `csv` is lighter.
- The upstream `show_tables()` signature currently expects `show_tables=True`.

## \[`csv`\]

`csv` is the lightweight CSV analysis toolkit for pre-registered files with optional DuckDB-backed SQL queries.

### What It Does

`csv` exposes `list_csv_files()`, `read_csv_file()`, `get_columns()`, and `query_csv_file()`.
The toolkit works with a preconfigured list of CSV paths and exposes each one by its filename stem.
`read_csv_file()` returns JSON rows and respects either the per-call `row_limit` or the configured default `row_limit`.
`query_csv_file()` loads the target CSV into DuckDB and runs one SQL statement against it.

### Configuration

| Option                  | Type      | Required | Default | Notes                                                                |
| ----------------------- | --------- | -------- | ------- | -------------------------------------------------------------------- |
| `csvs`                  | `text`    | `no`     | `null`  | Advanced pre-registered CSV path list passed through to the toolkit. |
| `row_limit`             | `number`  | `no`     | `null`  | Default row cap for `read_csv_file()`.                               |
| `duckdb_connection`     | `text`    | `no`     | `null`  | Advanced existing DuckDB connection object.                          |
| `duckdb_kwargs`         | `text`    | `no`     | `null`  | Advanced DuckDB connection kwargs mapping.                           |
| `enable_read_csv_file`  | `boolean` | `no`     | `true`  | Enable `read_csv_file()`.                                            |
| `enable_list_csv_files` | `boolean` | `no`     | `true`  | Enable `list_csv_files()`.                                           |
| `enable_get_columns`    | `boolean` | `no`     | `true`  | Enable `get_columns()`.                                              |
| `enable_query_csv_file` | `boolean` | `no`     | `true`  | Enable `query_csv_file()`.                                           |
| `all`                   | `boolean` | `no`     | `false` | Enable the full CSV toolkit.                                         |

### Example

```
agents:
  analyst:
    tools:
      - csv:
          row_limit: 200
```

```
list_csv_files()
read_csv_file("sales_2025", row_limit=50)
get_columns("sales_2025")
query_csv_file("sales_2025", 'SELECT "region", COUNT(*) FROM sales_2025 GROUP BY 1')
```

### Notes

- `csv` is most useful when the runtime or a higher-level wrapper has already pre-registered the CSV paths for the tool.
- `query_csv_file()` requires DuckDB and only runs the first SQL statement you provide.
- If you need richer file loading or exports, use `duckdb` instead.

## \[`pandas`\]

`pandas` is the in-memory dataframe toolkit for creating named dataframes and running dataframe methods on them.

### What It Does

`pandas` exposes `create_pandas_dataframe()` and `run_dataframe_operation()`.
`create_pandas_dataframe()` calls a top-level Pandas constructor such as `read_csv` or `read_json` and stores the resulting dataframe under a caller-chosen name.
`run_dataframe_operation()` then calls a dataframe method such as `head`, `tail`, `describe`, or `groupby` on that stored dataframe.
Stored dataframes live in the current process memory only and are not persisted to disk.

### Configuration

| Option                           | Type      | Required | Default | Notes                               |
| -------------------------------- | --------- | -------- | ------- | ----------------------------------- |
| `enable_create_pandas_dataframe` | `boolean` | `no`     | `true`  | Enable `create_pandas_dataframe()`. |
| `enable_run_dataframe_operation` | `boolean` | `no`     | `true`  | Enable `run_dataframe_operation()`. |
| `all`                            | `boolean` | `no`     | `false` | Enable the full Pandas toolkit.     |

### Example

```
agents:
  analyst:
    tools:
      - pandas
```

```
create_pandas_dataframe(
    "sales",
    "read_csv",
    {"filepath_or_buffer": "/workspace/data/sales.csv"},
)
run_dataframe_operation("sales", "head", {"n": 5})
run_dataframe_operation("sales", "describe", {})
```

### Notes

- The toolkit keeps dataframe state in memory on the current runtime process, so a restart clears it.
- `create_pandas_dataframe()` rejects empty dataframes and duplicate dataframe names.
- Use `pandas` when you want dataframe-native operations rather than SQL.

## \[`google_bigquery`\]

`google_bigquery` is the BigQuery toolkit for listing tables, describing schemas, and running SQL inside one dataset.

### What It Does

`google_bigquery` exposes `list_tables()`, `describe_table()`, and `run_sql_query()`.
The toolkit builds a `bigquery.Client` at initialization and scopes query jobs to `project.dataset` through the default query job configuration.
MindRoom's metadata marks `dataset`, `project`, and `location` as required, even though the upstream toolkit can fall back to `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` if those values are omitted.
`credentials` is an advanced programmatic Google credentials object for cases where the process should not rely on default application credentials.

### Configuration

| Option                  | Type      | Required | Default | Notes                                                                      |
| ----------------------- | --------- | -------- | ------- | -------------------------------------------------------------------------- |
| `dataset`               | `text`    | `yes`    | `null`  | BigQuery dataset name.                                                     |
| `project`               | `text`    | `yes`    | `null`  | Google Cloud project ID.                                                   |
| `location`              | `text`    | `yes`    | `null`  | BigQuery location such as `US` or `EU`.                                    |
| `credentials`           | `text`    | `no`     | `null`  | Advanced Google credentials object passed directly to the BigQuery client. |
| `enable_list_tables`    | `boolean` | `no`     | `true`  | Enable `list_tables()`.                                                    |
| `enable_describe_table` | `boolean` | `no`     | `true`  | Enable `describe_table()`.                                                 |
| `enable_run_sql_query`  | `boolean` | `no`     | `true`  | Enable `run_sql_query()`.                                                  |
| `all`                   | `boolean` | `no`     | `false` | Enable the full BigQuery toolkit.                                          |

### Example

```
agents:
  analyst:
    tools:
      - google_bigquery:
          project: my-gcp-project
          dataset: analytics
          location: US
```

```
list_tables()
describe_table("events")
run_sql_query("SELECT event_name, COUNT(*) AS total FROM events GROUP BY 1 ORDER BY total DESC LIMIT 20")
```

### Notes

- Configure `dataset`, `project`, and `location` explicitly in MindRoom, because that is the documented and validated path in the live metadata.
- `google_bigquery` does not use the shared Google Services OAuth integration.
- If `credentials` is unset, the BigQuery SDK falls back to the process's default Google Cloud authentication behavior.

## \[`google_sheets`\]

`google_sheets` is the Google Sheets toolkit for spreadsheet access through the Google Sheets OAuth provider.

### What It Does

The underlying Agno toolkit supports `read_sheet()`, `create_sheet()`, `update_sheet()`, and `create_duplicate_sheet()`.
MindRoom wraps Agno's Google Sheets toolkit with `ScopedOAuthClientMixin`, so it loads stored Google credentials from MindRoom's credential store instead of relying only on local token files.
The upstream toolkit selects read-only Sheets scope when only reads are enabled, and it selects write scope when create, update, or duplicate operations are enabled.
`create_duplicate_sheet()` uses the Google Drive copy API under the hood, so duplication depends on the provider's `drive.file` scope in addition to Sheets access.
If `spreadsheet_id` or `spreadsheet_range` is unset, you can still pass them per call.
MindRoom maps the dashboard config fields `read`, `create`, `update`, and `duplicate` onto Agno's constructor flags.

### Configuration

| Option              | Type      | Required | Default | Notes                                                                                           |
| ------------------- | --------- | -------- | ------- | ----------------------------------------------------------------------------------------------- |
| `spreadsheet_id`    | `text`    | `no`     | `null`  | Default spreadsheet ID. Leave unset to work with multiple spreadsheets by passing IDs per call. |
| `spreadsheet_range` | `text`    | `no`     | `null`  | Default range such as `Sheet1!A1:Z100`.                                                         |
| `read`              | `boolean` | `no`     | `true`  | Enable read operations.                                                                         |
| `create`            | `boolean` | `no`     | `false` | Enable spreadsheet creation.                                                                    |
| `update`            | `boolean` | `no`     | `false` | Enable sheet updates.                                                                           |
| `duplicate`         | `boolean` | `no`     | `false` | Enable spreadsheet duplication.                                                                 |

### Example

```
agents:
  ops:
    worker_scope: shared
    tools:
      - google_sheets:
          spreadsheet_id: 1AbCdEfGhIjKlMnOpQrStUvWxYz
          spreadsheet_range: Sheet1!A1:G200
```

```
read_sheet()
read_sheet(
    spreadsheet_id="1AbCdEfGhIjKlMnOpQrStUvWxYz",
    spreadsheet_range="Sheet1!A1:B20",
)
```

### Notes

- `google_sheets` uses the `google_sheets` OAuth provider and can use scoped OAuth credentials for isolated worker scopes.
- Configure Google OAuth through [Google Services OAuth (Admin Setup)](https://docs.mindroom.chat/deployment/google-services-oauth/index.md) or [Google Services OAuth (Individual Setup)](https://docs.mindroom.chat/deployment/google-services-user-oauth/index.md).
- The dashboard marks the tool available only when stored Google Sheets credentials include the required Sheets and Drive scopes.

## \[`openbb`\]

`openbb` is the finance toolkit for stock quotes, symbol lookup, company news, company profiles, and price targets through OpenBB providers.

### What It Does

`openbb` exposes `get_stock_price()`, `search_company_symbol()`, `get_company_news()`, `get_company_profile()`, and `get_price_targets()`.
The toolkit logs into OpenBB when `openbb_pat` or `OPENBB_PAT` is available, but it still works without a PAT when the selected provider supports unauthenticated access.
The default provider is `yfinance`, which makes the tool usable without premium OpenBB credentials for many common quote lookups.
You can selectively enable additional research functions without exposing the full OpenBB surface.

### Configuration

| Option                         | Type      | Required | Default    | Notes                                                                                            |
| ------------------------------ | --------- | -------- | ---------- | ------------------------------------------------------------------------------------------------ |
| `obb`                          | `text`    | `no`     | `null`     | Advanced preconfigured OpenBB instance object.                                                   |
| `openbb_pat`                   | `text`    | `no`     | `null`     | Optional OpenBB PAT for premium providers. Usually stored in credentials instead of inline YAML. |
| `provider`                     | `text`    | `no`     | `yfinance` | Data provider such as `yfinance`, `benzinga`, `fmp`, `intrinio`, `polygon`, `tiingo`, or `tmx`.  |
| `enable_get_stock_price`       | `boolean` | `no`     | `true`     | Enable `get_stock_price()`.                                                                      |
| `enable_search_company_symbol` | `boolean` | `no`     | `false`    | Enable `search_company_symbol()`.                                                                |
| `enable_get_company_news`      | `boolean` | `no`     | `false`    | Enable `get_company_news()`.                                                                     |
| `enable_get_company_profile`   | `boolean` | `no`     | `false`    | Enable `get_company_profile()`.                                                                  |
| `enable_get_price_targets`     | `boolean` | `no`     | `false`    | Enable `get_price_targets()`.                                                                    |
| `all`                          | `boolean` | `no`     | `false`    | Enable the full OpenBB toolkit.                                                                  |

### Example

```
agents:
  market:
    tools:
      - openbb:
          provider: yfinance
          enable_search_company_symbol: true
          enable_get_company_news: true
```

```
get_stock_price("AAPL,MSFT")
search_company_symbol("Nvidia")
get_company_news("AAPL", num_stories=5)
get_company_profile("MSFT")
get_price_targets("NVDA")
```

### Notes

- `openbb_pat` is optional, and the default `provider: yfinance` keeps the tool useful even without premium OpenBB access.
- Use `obb` only when you are constructing the toolkit programmatically with a live OpenBB object.
- This tool overlaps with `yfinance`, but `openbb` is the better fit when you want provider switching or OpenBB-specific data sources.

## \[`yfinance`\]

`yfinance` is the direct Yahoo Finance toolkit for quotes, company info, fundamentals, statements, recommendations, news, and price history.

### What It Does

`yfinance` exposes `get_current_stock_price()`, `get_company_info()`, `get_historical_stock_prices()`, `get_stock_fundamentals()`, `get_income_statements()`, `get_key_financial_ratios()`, `get_analyst_recommendations()`, `get_company_news()`, and `get_technical_indicators()`.
Unlike `openbb`, it does not require a PAT or provider selection.
The optional `session` field is an advanced programmatic HTTP session hook for callers that need custom transport behavior.

### Configuration

| Option    | Type   | Required | Default | Notes                                      |
| --------- | ------ | -------- | ------- | ------------------------------------------ |
| `session` | `text` | `no`     | `null`  | Advanced programmatic HTTP session object. |

### Example

```
agents:
  market:
    tools:
      - yfinance
```

```
get_current_stock_price("AAPL")
get_company_info("MSFT")
get_historical_stock_prices("NVDA", period="6mo", interval="1d")
get_company_news("TSLA", num_stories=5)
```

### Notes

- `yfinance` has no required setup fields for normal use.
- `session` is only useful for programmatic customization and is not a common `config.yaml` setting.
- Choose `yfinance` when you want the widest Yahoo Finance surface with the fewest setup requirements.

## \[`financial_datasets_api`\]

`financial_datasets_api` is the structured market-data toolkit for company info, statements, ownership, filings, news, search, and crypto prices.

### What It Does

`financial_datasets_api` exposes methods such as `get_income_statements()`, `get_balance_sheets()`, `get_cash_flow_statements()`, `get_segmented_financials()`, `get_financial_metrics()`, `get_company_info()`, `get_stock_prices()`, `get_earnings()`, `get_insider_trades()`, `get_institutional_ownership()`, `get_news()`, `get_sec_filings()`, `get_crypto_prices()`, and `search_tickers()`.
The toolkit sends authenticated HTTP requests to `https://api.financialdatasets.ai` with `X-API-KEY`.
If `api_key` is unset, it falls back to `FINANCIAL_DATASETS_API_KEY`, and otherwise the tool returns an API-key-not-set error instead of working partially.

### Configuration

| Option    | Type       | Required | Default | Notes                                                                                      |
| --------- | ---------- | -------- | ------- | ------------------------------------------------------------------------------------------ |
| `api_key` | `password` | `no`     | `null`  | Financial Datasets API key stored through the dashboard, credential store, or environment. |

### Example

```
agents:
  market:
    tools:
      - financial_datasets_api
```

```
search_tickers("cloud software", limit=5)
get_company_info("MSFT")
get_stock_prices("AAPL", interval="1d", limit=30)
get_income_statements("NVDA", period="quarterly", limit=4)
get_news("TSLA", limit=10)
```

### Notes

- Configure the API key through the dashboard or credential store, or provide `FINANCIAL_DATASETS_API_KEY` in the environment.
- Use this tool when you need more structured financial datasets than `yfinance` or the default OpenBB provider exposes.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/configuration/agents/#per-agent-tool-configuration)
- [Google Services OAuth (Admin Setup)](https://docs.mindroom.chat/deployment/google-services-oauth/index.md)
- [Google Services OAuth (Individual Setup)](https://docs.mindroom.chat/deployment/google-services-user-oauth/index.md)
