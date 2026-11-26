---
icon: lucide/database
---

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
| [`csv`](#csv) | Reading and querying a fixed list of CSV files | Paths of the CSV files |
| [`pandas`](#pandas) | In-memory dataframes and dataframe methods | None |
| [`google_bigquery`](#google_bigquery) | Tables and SQL in one BigQuery dataset | Project, dataset, location, Google Cloud connection or service account |
| [`google_drive`](#google_drive) | Listing, searching, reading, downloading, uploading, and organizing Drive files | Google Drive OAuth |
| [`google_docs`](#google_docs) | Creating, reading, and editing Google Docs | Google Docs OAuth |
| [`google_sheets`](#google_sheets) | Reading, creating, updating, and formatting spreadsheets | Google Sheets OAuth |
| [`openbb`](#openbb) | Stock quotes, symbol search, news, profiles, and price targets from switchable providers | Optional OpenBB PAT |
| [`yfinance`](#yfinance) | Yahoo Finance quotes, fundamentals, statements, news, and history | None |
| [`financial_datasets_api`](#financial_datasets_api) | Financial statements, filings, ownership, earnings, and crypto prices | API key |

## Setup and Trust

Tools that need a connection or account stay unavailable in the dashboard until their required fields or OAuth connection are stored.
Options of type `password` in the tables below cannot be set inline in `config.yaml`; see [Security Restrictions](index.md#security-restrictions).
`sql`, `postgres`, `redshift`, `duckdb`, `csv`, and `pandas` can read or write local files that the agent's `file_access` setting does not confine, and `sql`, `duckdb`, and `pandas` always run in the primary runtime, so enable them only for agents you trust with what the MindRoom process can reach; see [File access](../architecture/security-posture.md#file-access).
The Google Drive, Docs, and Sheets tools connect through per-service OAuth, and `google_bigquery` uses the Google Cloud connection; see [Google Services OAuth For Local Installs](../deployment/google-services-user-oauth.md) or [Google Services OAuth](../deployment/google-services-oauth.md) for custom and hosted setups.
Missing Python dependencies install automatically on first use; see [Automatic Dependency Installation](index.md#automatic-dependency-installation).

## [`sql`]

`sql` provides `sql_inspect(action, table_name=None)` (actions `list_tables`, `describe_table`) and `run_sql_query(query, limit=10)`.
`run_sql_query()` returns 10 rows unless the call passes another `limit`, or `limit=None` for all rows.
Connect with `db_url`, or let MindRoom assemble a URL from `dialect`, `user`, `password`, `host`, `port`, and `schema`.
The assembled URL uses `schema` as the database name and also as the schema for table inspection, so use `db_url` for dialects where those differ.
For PostgreSQL or Redshift, the dedicated [`postgres`](#postgres) and [`redshift`](#redshift) tools add query plans and exports.
PostgreSQL URLs require an explicit host (including Unix-socket `?host=...`), username, password, and database, and reject ambient libpq `passfile`, `PGSERVICE`, and `PGHOSTADDR` fallbacks.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `db_url` | `password` | `null` | SQLAlchemy connection URL, such as `postgresql://user:pass@host/db`. |
| `dialect` | `text` | `null` | SQLAlchemy dialect prefix such as `postgresql`, `mysql`, or `sqlite`. |
| `user` | `text` | `null` | Username for the assembled URL. |
| `password` | `password` | `null` | Password for the assembled URL. |
| `host` | `url` | `null` | Host for the assembled URL. |
| `port` | `number` | `null` | Port for the assembled URL. |
| `schema` | `text` | `null` | Database name in the assembled URL and schema for inspection. |
| `enable_list_tables` | `boolean` | `true` | Enable the `list_tables` action on `sql_inspect()`. |
| `enable_describe_table` | `boolean` | `true` | Enable the `describe_table` action on `sql_inspect()`. |
| `enable_run_sql_query` | `boolean` | `true` | Enable `run_sql_query()`. |
| `all` | `boolean` | `false` | Enable every function. |

## [`postgres`]

`postgres` provides `postgres_inspect(action, table=None, query=None)` (actions `show_tables`, `describe_table`, `summarize_table`, `inspect_query`), `run_query()`, and `export_table_to_path()`.
The connection is read-only, so queries cannot modify the database.
`postgres_inspect(action="inspect_query", ...)` runs `EXPLAIN`, which is a cheap check before a large `run_query()`.
`export_table_to_path()` writes a table as CSV to a path on the machine running MindRoom.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `host` | `url` | yes | `null` | Server hostname. |
| `port` | `number` | no | `5432` | Server port. |
| `db_name` | `text` | yes | `null` | Database name. |
| `user` | `text` | yes | `null` | Username. |
| `password` | `password` | yes | `null` | Password. |
| `table_schema` | `text` | no | `public` | Schema for table operations and the connection search path. |

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

`redshift` provides `redshift_inspect(action, table=None, query=None)` (actions `show_tables`, `describe_table`, `summarize_table`, `inspect_query`), `run_query()`, and `export_table_to_path()`.
It authenticates with `user` and `password`, or with IAM through an AWS `profile` or explicit AWS keys when `iam: true`.
Unset fields can fall back to the environment variables `REDSHIFT_HOST`, `REDSHIFT_DATABASE`, `REDSHIFT_DB_USER`, `REDSHIFT_CLUSTER_IDENTIFIER`, `AWS_REGION` (then `AWS_DEFAULT_REGION`), `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN`.
These are the standard AWS variables, so AWS values set for another client, such as [Amazon Bedrock Claude](../configuration/models.md#amazon-bedrock-claude), apply here too.
With IAM, a `profile` wins over explicit AWS keys, so an `AWS_PROFILE` in the environment replaces keys saved for this tool.
IAM mode requires an explicit `profile` or both `access_key_id` and `secret_access_key` rather than falling back to ambient AWS default credential chains.
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

`neo4j` provides `neo4j_inspect(action)` (actions `list_labels`, `list_relationship_types`, `get_schema`) and `run_cypher_query()`.
Set `enable_run_cypher: false` to give an agent schema visibility without free-form graph queries.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `uri` | `url` | no | `null` | Connection URI such as `bolt://localhost:7687`, or `NEO4J_URI`; set it in practice. |
| `user` | `text` | yes | `null` | Username, or `NEO4J_USERNAME`. |
| `password` | `password` | yes | `null` | Password, or `NEO4J_PASSWORD`. |
| `database` | `text` | no | `null` | Target database. |
| `enable_list_labels` | `boolean` | no | `true` | Enable the `list_labels` action on `neo4j_inspect()`. |
| `enable_list_relationships` | `boolean` | no | `true` | Enable the `list_relationship_types` action on `neo4j_inspect()`. |
| `enable_get_schema` | `boolean` | no | `true` | Enable the `get_schema` action on `neo4j_inspect()`. |
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
It provides `duckdb_inspect(action, ...)` (actions `show_tables`, `describe_table`, `summarize_table`, `inspect_query`, `full_text_search`), `run_query()`, and `duckdb_manage(action, ...)` (actions `load_path`, `export_table`, `create_fts_index`).
Without `db_path`, the database lives in memory and is not saved.
`duckdb_manage(action="export_table", ...)` writes Parquet unless the call passes `CSV` or `JSON`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `db_path` | `text` | `null` | Path to a persistent DuckDB database file. |
| `read_only` | `boolean` | `false` | Open the database read-only. |
| `init_commands` | `string[]` | `null` | SQL statements run in order when the tool opens its database, such as `INSTALL httpfs` and `LOAD httpfs`. |

```yaml
agents:
  analyst:
    tools:
      - duckdb:
          db_path: data/analytics.duckdb
```

```python
duckdb_manage("load_path", path="/workspace/data/orders.parquet", table="orders", replace=True)
run_query("SELECT customer_id, COUNT(*) AS orders FROM orders GROUP BY 1 ORDER BY orders DESC LIMIT 10")
duckdb_manage("export_table", table="orders", file_format="CSV", path="/tmp")
```

## [`csv`]

`csv` provides `csv_inspect(action, csv_name=None, row_limit=None)` (actions `list`, `columns`, `read`) and `query_csv_file()` over the CSV files listed in `csvs`, each named by its filename stem.
`query_csv_file()` runs only the first SQL statement it receives.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `csvs` | `string[]` | `null` | Absolute paths of the CSV files the tool reads. |
| `row_limit` | `number` | `null` | Default row cap for `csv_inspect(action="read", ...)`. |
| `enable_read_csv_file` | `boolean` | `true` | Enable the `read` action on `csv_inspect()`. |
| `enable_list_csv_files` | `boolean` | `true` | Enable the `list` action on `csv_inspect()`. |
| `enable_get_columns` | `boolean` | `true` | Enable the `columns` action on `csv_inspect()`. |
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

`google_bigquery` provides `list_tables()`, `describe_table(table_id)`, and `run_sql_query(query)` for the configured dataset.
The dataset is only the default for unqualified table names, so queries can still reference other datasets that the account can read.
It runs as the [Google Cloud connection](../deployment/google-services-oauth.md#providers) for the agent's credential scope, or as the service account in `GOOGLE_SERVICE_ACCOUNT_FILE` when that is configured, never as the MindRoom process's Application Default Credentials.
That account needs IAM access to read the data and to run query jobs in the project, such as the BigQuery Data Viewer and BigQuery Job User roles.
If the account is not connected, the tool returns an `OAuthConnectionRequired` result with a connect link.
The tool always runs in the primary runtime, even when `worker_tools` lists it.

`run_sql_query` accepts exactly one `SELECT` or `WITH` statement, with an optional trailing `;`.
It refuses DML, DDL, scripts, and multiple statements with `Only a single read-only SELECT query is allowed`.
It returns JSON with `columns`, `rows`, and `truncated`, and `truncated` is `true` when more than `max_rows` rows were available.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `project` | `text` | yes | `null` | Google Cloud project ID. |
| `dataset` | `text` | yes | `null` | Dataset name. |
| `location` | `text` | yes | `null` | Location such as `US` or `EU`. |
| `max_rows` | `number` | no | `100` | Most rows `run_sql_query` returns, from `1` to `1000`. |
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

<video controls playsinline preload="metadata" aria-label="The agent compares two Drive documents and flags a stale number" style="width: 100%" poster="https://github.com/user-attachments/assets/38ae5852-8273-4cd5-b05e-27e36c5ff157" data-poster-light="https://github.com/user-attachments/assets/38ae5852-8273-4cd5-b05e-27e36c5ff157" data-poster-dark="https://github.com/user-attachments/assets/08b198a5-3134-4b36-b688-4f431e87babf">
  <source src="https://github.com/user-attachments/assets/e5d0d6c6-f406-425a-a22d-570f0f4f45d5" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/ba7d6fcc-83e9-4a88-be2e-30ce9c8754e5" type="video/mp4">
</video>

`google_drive` works with files in the connected Google account, including shared drives the account can access.

| Function | Actions | What it does |
| --- | --- | --- |
| `google_drive_read(action, ...)` | `list`, `search`, `read`, `download` | Lists recently modified files, searches metadata with a Drive `query` such as `name contains 'budget'`, returns Google Docs, Sheets, Slides, Apps Script, and plain-text files up to `max_read_size` as text, or saves `file_id` into `google-drive-downloads/<file id>/` in the agent workspace (exporting Workspace files to a native format or `export_format`). |
| `google_drive_write(action, ...)` | `upload`, `update`, `create_folder`, `move`, `trash` | Uploads `local_path` (with optional `parent_id`, `name`, `mime_type`, `convert_to` `document`/`spreadsheet`/`presentation`), replaces contents of `file_id` keeping its ID and sharing, creates folders, moves or renames files, or moves a file to trash (never permanent delete). |

Use `google_drive_read(action="read", ...)` to read a document or text file, and `google_drive_read(action="download", ...)` for binary files such as audio, video, images, PDFs, archives, and Office files, for Drawings, and for text files over `max_read_size`.
Files with binary content, such as audio, video, images, PDFs, archives, and Office files, are never returned as text.
When `google_drive_read(action="read", ...)` refuses a file and the `download` action is enabled, the error suggests `google_drive_read(action="download")` if it can save the file, and otherwise quotes the reason it would refuse the file too, such as a folder, a Google Vids file, or a size over `max_download_size`.
Downloads larger than `max_download_size` are refused without leaving a partial file.
Files with the same name never overwrite each other, downloading the same file again under the same name replaces its earlier copy, and characters such as `/` in a Drive name become `_` in the saved name.
On an agent that is not `private`, every requester shares the agent workspace and can read what another person downloaded, so use a `private` agent when each person connects their own Google account.
Uploads and content replacement read local files according to the agent's [`file_access`](../architecture/security-posture.md#file-access) setting.
To make a formatted Google Doc, upload an HTML, Markdown, or Word file with `convert_to="document"`: Drive imports the formatting Google Docs supports, such as headings, tables, lists, and links.
Replacing the contents of a Google Doc, Sheet, or Slides file converts the local file the same way and replaces the whole file, so formatting that is not in the local file is lost and comments on the replaced content lose their place.
To change part of a Google Doc, use [`google_docs`](#google_docs).
If the account is not connected, the tool returns an `OAuthConnectionRequired` result with a connect link; see [Google Services OAuth For Local Installs](../deployment/google-services-user-oauth.md#connect).
A connection that granted only read-only Drive access keeps working for reads, while writes ask the user to reconnect; see [Scope Rationale](../deployment/google-services-oauth.md#scope-rationale).
Set the `GOOGLE_CLOUD_QUOTA_PROJECT_ID` environment variable to charge Drive API quota and billing to that Google Cloud project.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `list_files` | `boolean` | `true` | Enable `google_drive_read(action="list")`. |
| `search_files` | `boolean` | `true` | Enable `google_drive_read(action="search")`. |
| `read_file` | `boolean` | `true` | Enable `google_drive_read(action="read")`. |
| `download_file` | `boolean` | `true` | Enable `google_drive_read(action="download")` into the agent workspace. |
| `write` | `boolean` | `true` | Enable `google_drive_write()` (`upload`, `update`, `create_folder`, `move`, and `trash`); `false` removes it. |
| `max_read_size` | `number` | `10485760` | Largest non-Workspace file `google_drive_read(action="read")` returns as text, in bytes. |
| `max_download_size` | `number` | `104857600` | Largest file or export `google_drive_read(action="download")` saves, in bytes. |

To require a person to approve each Drive change, add a [Tool Approval](../tool-approval.md) rule:

```yaml
tool_approval:
  rules:
    - match: google_drive_write
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
| `google_docs_batch_update(document_id, requests, required_revision_id=None)` | Applies Google Docs API `batchUpdate` requests in order, all or none, for changes the text functions cannot make, such as tables, headings, styles, lists, links, and images. Requests can also delete content. Pass the `revisionId` from `google_docs_get_document` as `required_revision_id` to reject the whole batch if the document changed since that read. |

Each `document_id` accepts either a document ID or a full Google Docs URL.
Every successful call returns the document's edit URL.
If the account is not connected, the tool returns an `OAuthConnectionRequired` result with a connect link.
Before public production use, the Docs scope needs Google verification; see [Production Verification Follow-up](../deployment/google-services-oauth.md#production-verification-follow-up).

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `create_document` | `boolean` | `true` | Enable document creation. |
| `read_document` | `boolean` | `true` | Enable structure and content reads. |
| `edit_document` | `boolean` | `true` | Enable text insertion, text replacement, and batch updates. |

## [`google_sheets`]

`google_sheets` provides `google_sheets_read(spreadsheet_id=None, spreadsheet_range=None)` and `google_sheets_write(action, ...)` with actions `create`, `update`, and `batch_update`.
When `spreadsheet_id` or `spreadsheet_range` is configured, `google_sheets_read()` always uses it and ignores the value passed in the call, so leave both unset to read from many spreadsheets.
`google_sheets_write(action="update", ...)` ignores both settings and needs an explicit `spreadsheet_id` and `range_name` on every call.
`google_sheets_write(action="update", ...)` writes values literally, so a formula such as `=SUM(A1:A3)` is stored as text and does not calculate.
`google_sheets_write(action="batch_update", ...)` sends Sheets API [`batchUpdate`](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate) requests in order, for changes such as cell formatting, column widths, frozen rows, filters, and adding or renaming sheets.
`google_sheets_write(action="batch_update", ...)` can also delete sheets, rows, and ranges; add exact [Tool Approval](../tool-approval.md) rules for `google_sheets_write` and for each prefixed workspace function (such as `work_google_sheets_write`) to gate every Sheets write action:

```yaml
tool_approval:
  rules:
    - match: google_sheets_write
      action: require_approval
    - match: work_google_sheets_write
      action: require_approval
```

The tool is available only once the stored Google Sheets connection includes the Sheets scope; if the account is not connected, calls return an `OAuthConnectionRequired` result with a connect link.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `spreadsheet_id` | `text` | `null` | Spreadsheet ID used by every `google_sheets_read()` call. |
| `spreadsheet_range` | `text` | `null` | Range used by every `google_sheets_read()` call, such as `Sheet1!A1:Z100`. |
| `read` | `boolean` | `true` | Enable `google_sheets_read()`. |
| `create` | `boolean` | `true` | Enable `google_sheets_write(action="create")`. |
| `update` | `boolean` | `true` | Enable `google_sheets_write()` `update` and `batch_update` actions. |

```yaml
agents:
  ops:
    tools:
      - google_sheets:
          spreadsheet_id: 1AbCdEfGhIjKlMnOpQrStUvWxYz
          spreadsheet_range: Sheet1!A1:G200
```

## [`openbb`]

`openbb` provides `openbb_get(action, symbol=None, company_name=None, num_stories=10)` with actions `stock_price`, `search_symbol`, `company_news`, `company_profile`, and `price_targets`.
Symbols can be comma-separated, such as `AAPL,MSFT`.
The default `yfinance` provider works out of the box without credentials; credentialed providers are not supported via `openbb_pat` (or `OPENBB_PAT`) in OpenBB 4.7.2 (which removed `obb.account.login`) and fail closed rather than reading host environment variables or `~/.openbb_platform/user_settings.json`.
Choose `openbb` over [`yfinance`](#yfinance) when you need to switch data providers.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `provider` | `text` | `yfinance` | One of `yfinance`, `benzinga`, `fmp`, `intrinio`, `polygon`, `tiingo`, or `tmx`. |
| `openbb_pat` | `password` | `null` | OpenBB personal access token for premium providers. |
| `enable_get_stock_price` | `boolean` | `true` | Enable the `stock_price` action. |
| `enable_search_company_symbol` | `boolean` | `false` | Enable the `search_symbol` action. |
| `enable_get_company_news` | `boolean` | `false` | Enable the `company_news` action. |
| `enable_get_company_profile` | `boolean` | `false` | Enable the `company_profile` action. |
| `enable_get_price_targets` | `boolean` | `false` | Enable the `price_targets` action. |
| `all` | `boolean` | `false` | Enable every action. |

```yaml
agents:
  market:
    tools:
      - openbb:
          enable_search_company_symbol: true
          enable_get_company_news: true
```

## [`yfinance`]

`yfinance` reads Yahoo Finance data with no credentials through `yfinance_get(action, symbol, period=None, interval="1d", num_stories=3)`.
Only `stock_price` is enabled by default, so enable each additional action the agent needs.

| Option | Type | Default | Enables action |
| --- | --- | --- | --- |
| `enable_stock_price` | `boolean` | `true` | `stock_price` |
| `enable_company_info` | `boolean` | `false` | `company_info` |
| `enable_stock_fundamentals` | `boolean` | `false` | `fundamentals` |
| `enable_income_statements` | `boolean` | `false` | `income_statements` |
| `enable_key_financial_ratios` | `boolean` | `false` | `financial_ratios` |
| `enable_analyst_recommendations` | `boolean` | `false` | `analyst_recommendations` |
| `enable_company_news` | `boolean` | `false` | `company_news` |
| `enable_technical_indicators` | `boolean` | `false` | `technical_indicators` |
| `enable_historical_prices` | `boolean` | `false` | `historical_prices` |
| `all` | `boolean` | `false` | Every action |

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

`financial_datasets_api` reads structured data from Financial Datasets through `financial_datasets_financials(action, ticker, ...)` (actions `income_statements`, `balance_sheets`, `cash_flow_statements`, `segmented_financials`, `financial_metrics`, `company_info`, `earnings`, `insider_trades`, `institutional_ownership`, `sec_filings`) and `financial_datasets_market(action, ...)` (actions `stock_prices`, `crypto_prices`, `news`, `search_tickers`).
Set `api_key` or the `FINANCIAL_DATASETS_API_KEY` environment variable; without a key, every call returns `API key not set`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Financial Datasets API key. |
| `timeout` | `number` | `30` | Per-request HTTP timeout in seconds. |

## Related Docs

- [Tools Overview](index.md)
- [Per-Agent Tool Configuration](index.md#per-agent-tool-configuration)
- [Google Services OAuth For Local Installs](../deployment/google-services-user-oauth.md)
- [Google Services OAuth](../deployment/google-services-oauth.md)
