# Data & Databases

This page covers the built-in tools that query SQL and graph databases, analyze local data files, work with Google Drive, Docs, Sheets, and BigQuery, and fetch financial market data.
Use it to pick a tool, configure its connection, and understand why a tool is unavailable or refused a request.

## Choosing a Tool

| Tool | Use it for | Setup |
| --- | --- | --- |
| [`sql`](#sql) | Generic SQL over any database SQLAlchemy can open | Connection URL or fields |
| [`postgres`](#postgres) | Read-only PostgreSQL inspection, query plans, queries, and CSV export | Host, database, user, password |
| [`redshift`](#redshift) | Amazon Redshift inspection, queries, and export | Host, database, user, password |
| [`neo4j`](#neo4j) | Neo4j labels, relationship types, schema, and Cypher queries | URI, user, password |
| [`duckdb`](#duckdb) | Local analytical SQL over Parquet, CSV, JSON, and S3 files, with exports and full-text search | None |
| [`csv`](#csv) | SQL over pre-registered CSV files | Not configurable from `config.yaml`; use `duckdb` |
| [`pandas`](#pandas) | In-memory dataframes and dataframe methods | None |
| [`google_bigquery`](#google_bigquery) | Tables and SQL in one BigQuery dataset | Project, dataset, location, Google Cloud credentials |
| [`google_drive`](#google_drive) | Listing, searching, reading, downloading, uploading, and organizing Drive files | Google Drive OAuth |
| [`google_docs`](#google_docs) | Creating, reading, and editing Google Docs | Google Docs OAuth |
| [`google_sheets`](#google_sheets) | Reading, creating, updating, and formatting spreadsheets | Google Sheets OAuth |
| [`openbb`](#openbb) | Stock quotes, symbol search, news, profiles, and price targets from switchable providers | Optional OpenBB PAT |
| [`yfinance`](#yfinance) | Yahoo Finance quotes, fundamentals, statements, news, and history | None |
| [`financial_datasets_api`](#financial_datasets_api) | Financial statements, filings, ownership, earnings, and crypto prices | API key |

## Setup and Trust

Tools that need a connection or account stay unavailable in the dashboard until their required fields or OAuth connection are stored.
Options of type `password` in the tables below cannot be set inline in `config.yaml`; see [Security Restrictions](https://docs.mindroom.chat/tools/#security-restrictions).
`db_engine`, `tables`, `connection`, `init_commands`, `config`, `csvs`, `duckdb_connection`, `duckdb_kwargs`, `credentials`, and `obb` expect Python objects, lists, or mappings, so they cannot be set usefully from `config.yaml` or the dashboard.
`sql`, `postgres`, `redshift`, `duckdb`, `csv`, and `pandas` can read or write local files that the agent's `file_access` setting does not confine, and `sql`, `duckdb`, and `pandas` always run in the primary runtime, so enable them only for agents you trust with what the MindRoom process can reach; see [File access](https://docs.mindroom.chat/architecture/security-posture/#file-access).
The Google tools connect through per-service OAuth; see [Google Services OAuth For Local Installs](https://docs.mindroom.chat/deployment/google-services-user-oauth/) or [Google Services OAuth](https://docs.mindroom.chat/deployment/google-services-oauth/) for custom and hosted setups.
Missing Python dependencies install automatically on first use; see [Automatic Dependency Installation](https://docs.mindroom.chat/tools/#automatic-dependency-installation).

## [`sql`]

`sql` provides `list_tables()`, `describe_table(table_name)`, and `run_sql_query(query, limit=10)`.
`run_sql_query()` returns 10 rows unless the call passes another `limit`, or `limit=None` for all rows.
Connect with `db_url`, or let MindRoom assemble a URL from `dialect`, `user`, `password`, `host`, `port`, and `schema`.
The assembled URL uses `schema` as the database name and also as the schema for table inspection, so use `db_url` for dialects where those differ.
For PostgreSQL or Redshift, the dedicated [`postgres`](#postgres) and [`redshift`](#redshift) tools add query plans and exports.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `db_url` | `password` | `null` | SQLAlchemy connection URL, such as `postgresql://user:pass@host/db`. |
| `dialect` | `text` | `null` | SQLAlchemy dialect prefix such as `postgresql`, `mysql`, or `sqlite`. |
| `user` | `text` | `null` | Username for the assembled URL. |
| `password` | `password` | `null` | Password for the assembled URL. |
| `host` | `url` | `null` | Host for the assembled URL. |
| `port` | `number` | `null` | Port for the assembled URL. |
| `schema` | `text` | `null` | Database name in the assembled URL and schema for inspection. |
| `db_engine` | `text` | `null` | Programmatic only: a live SQLAlchemy `Engine`. |
| `tables` | `text` | `null` | Programmatic only: a table mapping that `list_tables()` returns instead of inspecting the database. |
| `enable_list_tables` | `boolean` | `true` | Enable `list_tables()`. |
| `enable_describe_table` | `boolean` | `true` | Enable `describe_table()`. |
| `enable_run_sql_query` | `boolean` | `true` | Enable `run_sql_query()`. |
| `all` | `boolean` | `false` | Enable every function. |

## [`postgres`]

`postgres` provides `show_tables()`, `describe_table()`, `summarize_table()`, `inspect_query()`, `run_query()`, and `export_table_to_path()`.
The connection is read-only, so queries cannot modify the database.
`inspect_query()` runs `EXPLAIN`, which is a cheap check before a large `run_query()`.
`export_table_to_path()` writes a table as CSV to a path on the machine running MindRoom.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `host` | `url` | yes | `null` | Server hostname. |
| `port` | `number` | no | `5432` | Server port. |
| `db_name` | `text` | yes | `null` | Database name. |
| `user` | `text` | yes | `null` | Username. |
| `password` | `password` | yes | `null` | Password. |
| `table_schema` | `text` | no | `public` | Schema for table operations and the connection search path. |
| `connection` | `text` | no | `null` | Programmatic only: an existing Psycopg connection. |

```yaml
agents:
  warehouse:
    tools:
      - postgres:
          host: warehouse.internal
          db_name: analytics
          user: analyst
          table_schema: reporting
```

## [`redshift`]

`redshift` provides the same functions as [`postgres`](#postgres): `show_tables()`, `describe_table()`, `summarize_table()`, `inspect_query()`, `run_query()`, and `export_table_to_path()`.
It authenticates with `user` and `password`, or with IAM through an AWS `profile` or explicit AWS keys when `iam: true`.
Unset fields can fall back to the environment variables `REDSHIFT_HOST`, `REDSHIFT_DATABASE`, `REDSHIFT_DB_USER`, `REDSHIFT_CLUSTER_IDENTIFIER`, `AWS_REGION`, and `AWS_PROFILE`.
The dashboard treats `user` and `password` as required even when `iam` is enabled, so store values for both before the tool becomes available.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `host` | `url` | yes | `null` | Cluster endpoint. |
| `port` | `number` | no | `5439` | Port. |
| `database` | `text` | yes | `null` | Database name. |
| `user` | `text` | yes | `null` | Username. |
| `password` | `password` | yes | `null` | Password. |
| `iam` | `boolean` | no | `false` | Use IAM authentication instead of the password. |
| `cluster_identifier` | `text` | no | `null` | Cluster identifier, required for IAM against provisioned clusters. |
| `region` | `text` | no | `null` | AWS region for IAM. |
| `db_user` | `text` | no | `null` | Database user for IAM. |
| `access_key_id` | `password` | no | `null` | AWS access key for IAM. |
| `secret_access_key` | `password` | no | `null` | AWS secret key for IAM. |
| `session_token` | `password` | no | `null` | AWS session token for temporary credentials. |
| `profile` | `text` | no | `null` | AWS profile name for IAM. |
| `ssl` | `boolean` | no | `true` | Use SSL. |
| `table_schema` | `text` | no | `public` | Schema for table operations. |

```yaml
agents:
  warehouse:
    tools:
      - redshift:
          host: my-cluster.abc123.us-east-1.redshift.amazonaws.com
          database: dev
          user: analyst
          table_schema: reporting
```

## [`neo4j`]

`neo4j` provides `list_labels()`, `list_relationship_types()`, `get_schema()`, and `run_cypher_query()`.
Set `enable_run_cypher: false` to give an agent schema visibility without free-form graph queries.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `uri` | `url` | no | `null` | Connection URI such as `bolt://localhost:7687`; set it in practice. |
| `user` | `text` | yes | `null` | Username. |
| `password` | `password` | yes | `null` | Password. |
| `database` | `text` | no | `null` | Target database. |
| `enable_list_labels` | `boolean` | no | `true` | Enable `list_labels()`. |
| `enable_list_relationships` | `boolean` | no | `true` | Enable `list_relationship_types()`. |
| `enable_get_schema` | `boolean` | no | `true` | Enable `get_schema()`. |
| `enable_run_cypher` | `boolean` | no | `true` | Enable `run_cypher_query()`. |
| `all` | `boolean` | no | `false` | Enable every function. |

```yaml
agents:
  graph:
    tools:
      - neo4j:
          uri: bolt://graph.internal:7687
          user: neo4j
          database: analytics
          enable_run_cypher: false
```

## [`duckdb`]

`duckdb` is the best choice for repeatable local analytics over files.
It provides `show_tables()`, `describe_table()`, `inspect_query()`, `run_query()`, `summarize_table()`, `create_table_from_path()`, `export_table_to_path()`, `load_local_path_to_table()`, `load_local_csv_to_table()`, `load_s3_path_to_table()`, `load_s3_csv_to_table()`, `create_fts_index()`, and `full_text_search()`.
Without `db_path`, the database lives in memory and is not saved.
`export_table_to_path()` writes Parquet unless the call passes another format such as `CSV`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `db_path` | `text` | `null` | Path to a persistent DuckDB database file. |
| `read_only` | `boolean` | `false` | Open the database read-only. |
| `connection` | `text` | `null` | Programmatic only: an existing DuckDB connection. |
| `init_commands` | `text` | `null` | Programmatic only: a list of startup SQL commands. |
| `config` | `text` | `null` | Programmatic only: a DuckDB config mapping. |

```yaml
agents:
  analyst:
    tools:
      - duckdb:
          db_path: data/analytics.duckdb
```

```python
create_table_from_path("/workspace/data/orders.parquet", table="orders", replace=True)
run_query("SELECT customer_id, COUNT(*) AS orders FROM orders GROUP BY 1 ORDER BY orders DESC LIMIT 10")
export_table_to_path("orders", format="CSV", path="/tmp")
```

## [`csv`]

`csv` provides `list_csv_files()`, `read_csv_file()`, `get_columns()`, and `query_csv_file()` over a pre-registered list of CSV files, each named by its filename stem.
`config.yaml` and the dashboard accept `csvs` only as a string, so they cannot register a file list and the tool has no files to read; use [`duckdb`](#duckdb) to query CSV files.
`query_csv_file()` runs only the first SQL statement it receives.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `csvs` | `text` | `null` | Programmatic only: the list of CSV paths. |
| `row_limit` | `number` | `null` | Default row cap for `read_csv_file()`. |
| `duckdb_connection` | `text` | `null` | Programmatic only: an existing DuckDB connection. |
| `duckdb_kwargs` | `text` | `null` | Programmatic only: DuckDB connection arguments. |
| `enable_read_csv_file` | `boolean` | `true` | Enable `read_csv_file()`. |
| `enable_list_csv_files` | `boolean` | `true` | Enable `list_csv_files()`. |
| `enable_get_columns` | `boolean` | `true` | Enable `get_columns()`. |
| `enable_query_csv_file` | `boolean` | `true` | Enable `query_csv_file()`. |
| `all` | `boolean` | `false` | Enable every function. |

## [`pandas`]

`pandas` provides `create_pandas_dataframe()` and `run_dataframe_operation()`.
`create_pandas_dataframe()` calls a Pandas constructor such as `read_csv` or `read_json` and stores the result under a name, rejecting empty dataframes and names already in use.
`run_dataframe_operation()` calls a dataframe method such as `head`, `describe`, or `groupby` on a stored dataframe.
Dataframes live only in memory and are lost on restart.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `enable_create_pandas_dataframe` | `boolean` | `true` | Enable `create_pandas_dataframe()`. |
| `enable_run_dataframe_operation` | `boolean` | `true` | Enable `run_dataframe_operation()`. |
| `all` | `boolean` | `false` | Enable every function. |

```python
create_pandas_dataframe("sales", "read_csv", {"filepath_or_buffer": "/workspace/data/sales.csv"})
run_dataframe_operation("sales", "describe", {})
```

## [`google_bigquery`]

`google_bigquery` provides `list_tables()`, `describe_table()`, and `run_sql_query()` for the configured dataset.
The dataset is only the default for unqualified table names, so queries can still reference other datasets that the Google Cloud credentials can read.
It authenticates with the MindRoom process's default Google Cloud credentials, not with MindRoom's Google OAuth connections.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `project` | `text` | yes | `null` | Google Cloud project ID. |
| `dataset` | `text` | yes | `null` | Dataset name. |
| `location` | `text` | yes | `null` | Location such as `US` or `EU`. |
| `credentials` | `text` | no | `null` | Programmatic only: a Google credentials object; rejected as an inline override. |
| `list_tables` | `boolean` | no | `true` | Enable `list_tables()`. |
| `describe_table` | `boolean` | no | `true` | Enable `describe_table()`. |
| `run_sql_query` | `boolean` | no | `true` | Enable `run_sql_query()`. |
| `all` | `boolean` | no | `false` | Enable every function. |

```yaml
agents:
  analyst:
    tools:
      - google_bigquery:
          project: my-gcp-project
          dataset: analytics
          location: US
```

## [`google_drive`]

<video controls playsinline preload="metadata" aria-label="The agent compares two Drive documents and flags a stale number" style="width: 100%" poster="https://github.com/user-attachments/assets/21a9b571-0a54-47e6-8050-391ec757ba55" data-poster-light="https://github.com/user-attachments/assets/21a9b571-0a54-47e6-8050-391ec757ba55" data-poster-dark="https://github.com/user-attachments/assets/ce6ffe08-b3c1-4bdd-9d75-8e3abbea1e65">
  <source src="https://github.com/user-attachments/assets/ffba1923-b463-4674-88af-5a862de1613f" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/690273d3-7fee-4e49-a1b5-75e39be00d8b" type="video/mp4">
</video>

`google_drive` works with files in the connected Google account, including shared drives the account can access.

| Function | What it does |
| --- | --- |
| `google_drive_list_files()` | Lists recently modified files. |
| `google_drive_search_files(query)` | Searches file metadata with a Drive query such as `name contains 'budget'`. |
| `google_drive_read_file(file_id)` | Returns Google Workspace files as text, and plain-text files up to `max_read_size`; PDFs and Office files need a download and a reader for that format. |
| `google_drive_download_file(file_id, export_format=None)` | Saves a file into `google-drive-downloads/` in the agent workspace, exporting Workspace files to a native format such as `.xlsx` for a whole spreadsheet, or to the MIME type in `export_format`, such as `application/pdf`. |
| `google_drive_upload_file(local_path, folder_id=None, name=None, mime_type=None)` | Uploads a local file; relative paths start at the agent workspace. |
| `google_drive_update_file(file_id, local_path, mime_type=None)` | Replaces the contents of an existing non-Workspace file. |
| `google_drive_create_folder(name, parent_id=None)` | Creates a folder under the Drive root or a parent folder. |
| `google_drive_move_file(file_id, new_parent_id, name=None)` | Moves a file and optionally renames it. |
| `google_drive_trash_file(file_id)` | Moves a file to trash; the tool never deletes permanently. |

Non-Workspace files over `max_read_size` return an error suggesting `google_drive_download_file` instead.
Downloads larger than `max_download_size` are refused without leaving a partial file.
Uploads and content replacement read local files according to the agent's [`file_access`](https://docs.mindroom.chat/architecture/security-posture/#file-access) setting.
Downloads need an agent workspace; for an agent without one, `download_file` is ignored and a warning is logged.
To change the content of a native Google Doc, use [`google_docs`](#google_docs).
If the account is not connected, the tool returns an `OAuthConnectionRequired` result with a connect link; see [Google Services OAuth For Local Installs](https://docs.mindroom.chat/deployment/google-services-user-oauth/#connect).
A connection that granted only read-only Drive access keeps working for reads, while writes ask the user to reconnect; see [Scope Rationale](https://docs.mindroom.chat/deployment/google-services-oauth/#scope-rationale).

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `list_files` | `boolean` | `true` | Enable file listing. |
| `search_files` | `boolean` | `true` | Enable metadata search. |
| `read_file` | `boolean` | `true` | Enable content reads. |
| `download_file` | `boolean` | `false` | Enable downloads and exports into the agent workspace. |
| `write` | `boolean` | `true` | Enable upload, content replacement, folder creation, move or rename, and trash; `false` removes all of them. |
| `max_read_size` | `number` | `10485760` | Largest non-Workspace file to read, in bytes. |
| `max_download_size` | `number` | `104857600` | Largest file or export to download, in bytes. |

To require a person to approve each Drive change, add [Tool Approval](https://docs.mindroom.chat/tool-approval/) rules:

```yaml
tool_approval:
  rules:
    - match: google_drive_upload_file
      action: require_approval
    - match: google_drive_update_file
      action: require_approval
    - match: google_drive_create_folder
      action: require_approval
    - match: google_drive_move_file
      action: require_approval
    - match: google_drive_trash_file
      action: require_approval
```

## [`google_docs`]

`google_docs` creates, reads, and edits Google Docs through its own OAuth connection, separate from Google Drive.

| Function | What it does |
| --- | --- |
| `google_docs_create_document(title, initial_text="")` | Creates a document, optionally with initial text. |
| `google_docs_get_document(document_id)` | Returns the full document structure and content, including every tab, paragraph, style, table, and list. |
| `google_docs_insert_text(document_id, text, index=None, tab_id=None)` | Inserts text at a body index (1 is the first position), or appends to the end of the tab when `index` is omitted. |
| `google_docs_replace_text(document_id, find_text, replace_text, match_case=False, tab_ids=None)` | Replaces every match across all tabs or the listed tabs and reports the count. |

Each `document_id` accepts either a document ID or a full Google Docs URL.
Every successful call returns the document's edit URL.
If the account is not connected, the tool returns an `OAuthConnectionRequired` result with a connect link.
Before public production use, the Docs scope needs Google verification; see [Production Verification Follow-up](https://docs.mindroom.chat/deployment/google-services-oauth/#production-verification-follow-up).

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `create_document` | `boolean` | `true` | Enable document creation. |
| `read_document` | `boolean` | `true` | Enable structure and content reads. |
| `edit_document` | `boolean` | `true` | Enable text insertion and replacement. |

## [`google_sheets`]

`google_sheets` provides `read_sheet(spreadsheet_id=None, spreadsheet_range=None)`, `create_sheet(title)`, `update_sheet(data, spreadsheet_id=None, range_name=None)`, and `batch_update_sheet(spreadsheet_id, requests)`.
When `spreadsheet_id` or `spreadsheet_range` is configured, `read_sheet()` always uses it and ignores the value passed in the call, so leave both unset to read from many spreadsheets.
`update_sheet()` ignores both settings and needs an explicit `spreadsheet_id` and `range_name` on every call.
`update_sheet()` writes values literally, so a formula such as `=SUM(A1:A3)` is stored as text and does not calculate.
`batch_update_sheet()` sends Sheets API [`batchUpdate`](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate) requests in order, for changes such as cell formatting, column widths, frozen rows, filters, and adding or renaming sheets.
`batch_update_sheet()` can also delete sheets, rows, and ranges, and a [Tool Approval](https://docs.mindroom.chat/tool-approval/) rule for `update_sheet` does not match it, so gate both names to require approval for every change to an existing spreadsheet.
The tool is available only once the stored Google Sheets connection includes the Sheets scope; if the account is not connected, calls return an `OAuthConnectionRequired` result with a connect link.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `spreadsheet_id` | `text` | `null` | Spreadsheet ID used by every `read_sheet()` call. |
| `spreadsheet_range` | `text` | `null` | Range used by every `read_sheet()` call, such as `Sheet1!A1:Z100`. |
| `read` | `boolean` | `true` | Enable `read_sheet()`. |
| `create` | `boolean` | `true` | Enable `create_sheet()`. |
| `update` | `boolean` | `true` | Enable `update_sheet()` and `batch_update_sheet()`. |

```yaml
agents:
  ops:
    tools:
      - google_sheets:
          spreadsheet_id: 1AbCdEfGhIjKlMnOpQrStUvWxYz
          spreadsheet_range: Sheet1!A1:G200
```

## [`openbb`]

`openbb` provides `get_stock_price(symbol)`, `search_company_symbol()`, `get_company_news(symbol, num_stories=10)`, `get_company_profile()`, and `get_price_targets()`.
Symbols can be comma-separated, such as `AAPL,MSFT`.
The default `yfinance` provider works without an OpenBB account; set `openbb_pat` or the `OPENBB_PAT` environment variable for premium providers.
Choose `openbb` over [`yfinance`](#yfinance) when you need to switch data providers.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `provider` | `text` | `yfinance` | One of `yfinance`, `benzinga`, `fmp`, `intrinio`, `polygon`, `tiingo`, or `tmx`. |
| `openbb_pat` | `password` | `null` | OpenBB personal access token for premium providers. |
| `obb` | `text` | `null` | Programmatic only: a configured OpenBB instance. |
| `enable_get_stock_price` | `boolean` | `true` | Enable `get_stock_price()`. |
| `enable_search_company_symbol` | `boolean` | `false` | Enable `search_company_symbol()`. |
| `enable_get_company_news` | `boolean` | `false` | Enable `get_company_news()`. |
| `enable_get_company_profile` | `boolean` | `false` | Enable `get_company_profile()`. |
| `enable_get_price_targets` | `boolean` | `false` | Enable `get_price_targets()`. |
| `all` | `boolean` | `false` | Enable every function. |

```yaml
agents:
  market:
    tools:
      - openbb:
          enable_search_company_symbol: true
          enable_get_company_news: true
```

## [`yfinance`]

`yfinance` reads Yahoo Finance data with no credentials.
Only the current stock price is enabled by default, so enable each additional function the agent needs.

| Option | Type | Default | Enables |
| --- | --- | --- | --- |
| `enable_stock_price` | `boolean` | `true` | `get_current_stock_price()` |
| `enable_company_info` | `boolean` | `false` | `get_company_info()` |
| `enable_stock_fundamentals` | `boolean` | `false` | `get_stock_fundamentals()` |
| `enable_income_statements` | `boolean` | `false` | `get_income_statements()` |
| `enable_key_financial_ratios` | `boolean` | `false` | `get_key_financial_ratios()` |
| `enable_analyst_recommendations` | `boolean` | `false` | `get_analyst_recommendations()` |
| `enable_company_news` | `boolean` | `false` | `get_company_news()` |
| `enable_technical_indicators` | `boolean` | `false` | `get_technical_indicators()` |
| `enable_historical_prices` | `boolean` | `false` | `get_historical_stock_prices()` |
| `all` | `boolean` | `false` | Every function |

```yaml
agents:
  market:
    tools:
      - yfinance:
          enable_company_info: true
          enable_historical_prices: true
          enable_company_news: true
```

## [`financial_datasets_api`]

`financial_datasets_api` reads structured data from Financial Datasets through `get_income_statements()`, `get_balance_sheets()`, `get_cash_flow_statements()`, `get_segmented_financials()`, `get_financial_metrics()`, `get_company_info()`, `get_stock_prices()`, `get_earnings()`, `get_insider_trades()`, `get_institutional_ownership()`, `get_news()`, `get_sec_filings()`, `get_crypto_prices()`, and `search_tickers()`.
Set `api_key` or the `FINANCIAL_DATASETS_API_KEY` environment variable; without a key, every call returns `API key not set`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Financial Datasets API key. |
| `timeout` | `number` | `30` | Per-request HTTP timeout in seconds. |

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/tools/#per-agent-tool-configuration)
- [Google Services OAuth For Local Installs](https://docs.mindroom.chat/deployment/google-services-user-oauth/)
- [Google Services OAuth](https://docs.mindroom.chat/deployment/google-services-oauth/)
