"""DuckDB tool configuration and native implementation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import (
    UNSET,
    escape_sql_literal,
    first_sql_statement,
    format_tabular_rows,
    quote_sql_identifier,
    require_non_empty,
    resolve_bool_toggle,
)

logger = get_logger(__name__)

_DuckDbInspectAction = Literal["show_tables", "describe_table", "summarize_table", "inspect_query", "full_text_search"]
_DuckDbManageAction = Literal["load_path", "export_table", "create_fts_index"]
_DuckDbExportFormat = Literal["PARQUET", "CSV", "JSON"]
_ALLOWED_EXPORT_FORMATS = frozenset({"PARQUET", "CSV", "JSON"})


class _DuckDbTools(Toolkit):
    """Run analytical SQL, inspect schemas, and load/export local or S3 data in DuckDB."""

    def __init__(
        self,
        db_path: str | None = None,
        connection: Any | None = None,  # noqa: ANN401 - optional caller-supplied DuckDB connection
        init_commands: list[str] | None = None,
        read_only: bool | None = UNSET,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.db_path = db_path
        self.read_only = resolve_bool_toggle(read_only, default=False)
        self.config = config
        self._connection: Any | None = connection
        self.init_commands = list(init_commands) if init_commands is not None else None
        self._reserved_keywords: frozenset[str] | None = None

        super().__init__(
            name="duckdb_tools",
            tools=[self.duckdb_inspect, self.run_query, self.duckdb_manage],
        )

    @property
    def connection(self) -> Any:  # noqa: ANN401 - duckdb.DuckDBPyConnection is an optional runtime import
        """Open and return the DuckDB connection lazily, running configured init commands once."""
        if self._connection is None:
            import duckdb

            kwargs: dict[str, Any] = {}
            if self.db_path is not None:
                kwargs["database"] = self.db_path
            if self.read_only:
                kwargs["read_only"] = True
            if self.config is not None:
                kwargs["config"] = self.config
            self._connection = duckdb.connect(**kwargs)
            if self.init_commands is not None:
                try:
                    for command in self.init_commands:
                        self._connection.sql(command)
                except Exception as exc:
                    logger.warning("duckdb_init_commands_failed", error=str(exc))
        return self._connection

    def _reserved_keywords_set(self) -> frozenset[str]:
        if self._reserved_keywords is None:
            rows = self.connection.sql(
                "SELECT keyword_name FROM duckdb_keywords() WHERE keyword_category = 'reserved'",
            ).fetchall()
            self._reserved_keywords = frozenset(str(row[0]).lower() for row in rows)
        return self._reserved_keywords

    def _table_name_from_path(self, path: str) -> str:
        table = re.sub(r"\W+", "_", Path(path).stem).strip("_")
        if not table:
            table = "tbl"
        if table[0].isdigit():
            table = f"_{table}"
        if table.lower() in self._reserved_keywords_set():
            table = f"{table}_"
        return table

    @staticmethod
    def _fts_schema_identifier(table: str) -> str:
        parts = [part for part in table.strip().split(".") if part]
        schema = parts[0] if len(parts) > 1 else "main"
        name = f"fts_{schema}_{parts[-1]}"
        return quote_sql_identifier(name)

    def _execute_sql(self, statement: str) -> str:
        query_result = self.connection.sql(statement)
        if query_result is None:
            return "No output"
        try:
            rows = query_result.fetchall()
            return format_tabular_rows(query_result.columns, rows)
        except AttributeError:
            return str(query_result)

    def run_query(self, query: str) -> str:
        """Run a single DuckDB SQL query and return CSV-formatted output."""
        formatted_sql = first_sql_statement(query)
        if not formatted_sql:
            return "Error: SQL query is empty"
        try:
            return self._execute_sql(formatted_sql)
        except Exception as exc:
            return str(exc)

    def duckdb_inspect(
        self,
        action: _DuckDbInspectAction,
        table: str | None = None,
        query: str | None = None,
        unique_key: str | None = None,
        search_text: str | None = None,
    ) -> str:
        """Inspect DuckDB tables, query plans, and full-text search indexes.

        Actions:
        - `show_tables`: list tables in the database.
        - `describe_table`: describe columns and types of `table`.
        - `summarize_table`: compute summary aggregates for `table`.
        - `inspect_query`: return `EXPLAIN` plan for `query`.
        - `full_text_search`: run BM25 search on indexed `table` using `unique_key` and `search_text`.
        """
        try:
            result = self._run_inspect(action, table=table, query=query, unique_key=unique_key, search_text=search_text)
        except Exception as exc:
            return f"Error in duckdb_inspect ({action}): {exc}"
        return result

    def _run_inspect(
        self,
        action: _DuckDbInspectAction,
        *,
        table: str | None,
        query: str | None,
        unique_key: str | None,
        search_text: str | None,
    ) -> str:
        if action == "show_tables":
            return self._execute_sql("SHOW TABLES")
        if action == "describe_table":
            table_name = require_non_empty(table, "table")
            return f"{table_name}\n{self._execute_sql(f'DESCRIBE {quote_sql_identifier(table_name)}')}"
        if action == "summarize_table":
            table_name = require_non_empty(table, "table")
            return self._execute_sql(f"SUMMARIZE {quote_sql_identifier(table_name)}")
        if action == "inspect_query":
            stmt = first_sql_statement(require_non_empty(query, "query"))
            return self._execute_sql(f"EXPLAIN {stmt}")
        if action == "full_text_search":
            table_name = require_non_empty(table, "table")
            key_ident = quote_sql_identifier(require_non_empty(unique_key, "unique_key"))
            safe_text = escape_sql_literal(require_non_empty(search_text, "search_text"))
            fts_schema = self._fts_schema_identifier(table_name)
            table_ident = quote_sql_identifier(table_name)
            stmt = (
                f"SELECT {fts_schema}.match_bm25({key_ident}, '{safe_text}') AS score, * "  # noqa: S608 - quoted/escaped
                f"FROM {table_ident} WHERE score IS NOT NULL ORDER BY score DESC"
            )
            return self._execute_sql(stmt)
        return f"Unsupported action: {action}"

    def duckdb_manage(
        self,
        action: _DuckDbManageAction,
        path: str | None = None,
        table: str | None = None,
        replace: bool = False,
        delimiter: str | None = None,
        file_format: _DuckDbExportFormat = "PARQUET",
        unique_key: str | None = None,
        columns: list[str] | None = None,
    ) -> str:
        """Load files into DuckDB, export tables, or build full-text search indexes.

        Actions:
        - `load_path`: load local or S3 CSV/Parquet/JSON `path` into `table` (derived from filename if omitted); set `replace=True` to overwrite, `delimiter` for custom CSV delimiters.
        - `export_table`: export `table` to `path` (directory or current dir if omitted) as `PARQUET`, `CSV`, or `JSON`.
        - `create_fts_index`: install/load `fts` and index `columns` of `table` keyed by `unique_key`.
        """
        try:
            result = self._run_manage(
                action,
                path=path,
                table=table,
                replace=replace,
                delimiter=delimiter,
                file_format=file_format,
                unique_key=unique_key,
                columns=columns,
            )
        except Exception as exc:
            return f"Error in duckdb_manage ({action}): {exc}"
        return result

    def _run_manage(
        self,
        action: _DuckDbManageAction,
        *,
        path: str | None,
        table: str | None,
        replace: bool,
        delimiter: str | None,
        file_format: _DuckDbExportFormat,
        unique_key: str | None,
        columns: list[str] | None,
    ) -> str:
        if action == "load_path":
            source_path = require_non_empty(path, "path")
            table_name = (
                table.strip() if isinstance(table, str) and table.strip() else self._table_name_from_path(source_path)
            )
            table_ident = quote_sql_identifier(table_name)
            safe_path = escape_sql_literal(source_path)
            prefix = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE IF NOT EXISTS"
            if source_path.lower().endswith(".csv") or delimiter is not None:
                delim_clause = f", delim='{escape_sql_literal(delimiter)}'" if delimiter is not None else ""
                select_sql = (
                    f"SELECT * FROM read_csv('{safe_path}', ignore_errors=false, auto_detect=true{delim_clause})"  # noqa: S608 - escaped
                )
            else:
                select_sql = f"SELECT * FROM '{safe_path}'"  # noqa: S608 - escaped
            self._execute_sql(f"{prefix} {table_ident} AS {select_sql}")
            return table_name

        if action == "export_table":
            table_name = require_non_empty(table, "table")
            normalized_format = file_format.strip().upper()
            if normalized_format not in _ALLOWED_EXPORT_FORMATS:
                return f"Error: Unsupported export format {file_format!r}. Use PARQUET, CSV, or JSON."
            ext = normalized_format.lower()
            output_path = f"{path}/{table_name}.{ext}" if path else f"{table_name}.{ext}"
            stmt = (
                f"COPY (SELECT * FROM {quote_sql_identifier(table_name)}) "  # noqa: S608 - quoted/escaped/allowlisted format
                f"TO '{escape_sql_literal(output_path)}' (FORMAT {normalized_format})"
            )
            return self._execute_sql(stmt)

        if action == "create_fts_index":
            table_name = require_non_empty(table, "table")
            key_name = require_non_empty(unique_key, "unique_key")
            if not columns:
                return "Error: columns is required for create_fts_index"
            self._execute_sql("INSTALL fts")
            self._execute_sql("LOAD fts")
            col_literals = ", ".join(f"'{escape_sql_literal(col)}'" for col in columns)
            stmt = (
                f"PRAGMA create_fts_index('{escape_sql_literal(table_name)}', "
                f"'{escape_sql_literal(key_name)}', {col_literals})"
            )
            return self._execute_sql(stmt)

        return f"Unsupported action: {action}"


@register_tool_with_metadata(
    name="duckdb",
    file_access=ToolFileAccess.UNCONFINED,
    requires_primary_runtime=True,
    display_name="DuckDB",
    description="In-memory analytical database for data processing and analysis",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Database",
    icon_color="text-yellow-600",
    config_fields=[
        ConfigField(
            name="db_path",
            label="Db Path",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="init_commands",
            label="Init Commands",
            type="string[]",
            required=False,
            default=None,
            description="SQL statements run, in order, when the tool opens its database connection.",
        ),
        ConfigField(
            name="read_only",
            label="Read Only",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["duckdb"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#duckdb",
    function_names=("duckdb_inspect", "duckdb_manage", "run_query"),
    action_scoped_grants=("duckdb_inspect", "duckdb_manage"),
)
def duckdb_tools() -> type[Toolkit]:
    """Return DuckDB tools for data analysis and processing."""
    return _DuckDbTools
