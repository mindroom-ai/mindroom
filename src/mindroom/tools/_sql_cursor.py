"""Shared SQL cursor base toolkit for PostgreSQL and Amazon Redshift."""

from __future__ import annotations

import csv
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, LiteralString

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools.native_helpers import format_tabular_rows, require_non_empty

logger = get_logger(__name__)

SqlInspectAction = Literal["show_tables", "describe_table", "summarize_table", "inspect_query"]
_TEXT_TYPES = ("char", "text", "uuid")


class SqlCursorToolkit(Toolkit):
    """Shared DB-API cursor lifecycle, query, inspection, and CSV export helpers."""

    requires_connect = True
    _db_label: str = "SQL"
    _inspect_tool_name: str = "sql_inspect"
    _numeric_types: tuple[str, ...] = (
        "integer",
        "numeric",
        "real",
        "double precision",
        "bigint",
        "smallint",
    )
    _text_stats_sql: LiteralString = (
        "SELECT COUNT(*) AS total_rows, COUNT({col}) AS non_null_rows, "
        "COUNT(DISTINCT {col}) AS unique_values, AVG(LENGTH({col})) AS avg_length "
        "FROM {tbl};"
    )

    def __init__(
        self,
        *,
        name: str,
        tools: list[Any],
        table_schema: str = "public",
        connection: Any | None = None,  # noqa: ANN401 - optional caller-supplied DB-API connection
    ) -> None:
        self._connection: Any | None = connection
        self._owns_connection: bool = False
        self.table_schema = table_schema or "public"
        super().__init__(name=name, tools=tools)

    def _open_connection(self) -> Any:  # noqa: ANN401 - subclass returns DB-API connection
        raise NotImplementedError

    def _format_sql_query(
        self,
        template: LiteralString,
        *,
        table: str,
        col: str | None = None,
    ) -> Any:  # noqa: ANN401 - returns str or psycopg.sql.Composable
        raise NotImplementedError

    def _empty_result_message(self, cursor: Any, /) -> str:  # noqa: ANN401, ARG002 - DB-API cursor
        return "Query executed successfully."

    def _should_rollback(self, exc: Exception) -> bool:  # noqa: ARG002
        return True

    def _rollback_if_open(self, connection: Any | None, exc: Exception) -> None:  # noqa: ANN401
        if connection is not None and self._should_rollback(exc) and not getattr(connection, "closed", False):
            with suppress(Exception):
                connection.rollback()

    def _ensure_connection(self) -> Any:  # noqa: ANN401
        if self._connection is None or getattr(self._connection, "closed", False):
            self._connection = self._open_connection()
            self._owns_connection = True
        return self._connection

    def connect(self) -> None:
        """Open the database connection for the run."""
        self._ensure_connection()

    def close(self) -> None:
        """Close and clear a toolkit-owned database connection."""
        connection = self._connection
        owns = self._owns_connection
        self._connection = None
        self._owns_connection = False
        if owns and connection is not None and not getattr(connection, "closed", False):
            with suppress(Exception):
                connection.close()

    def _run_db_query(self, query: str, params: tuple[object, ...] | None = None) -> str:
        connection: Any | None = None
        try:
            connection = self._ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                if cursor.description is None:
                    return self._empty_result_message(cursor)
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                if not rows:
                    return f"Query returned no results.\nColumns: {', '.join(columns)}"
                row_values = [tuple(row.values()) if isinstance(row, dict) else tuple(row) for row in rows]
                return format_tabular_rows(columns, row_values)
        except Exception as exc:
            self._rollback_if_open(connection, exc)
            logger.exception(f"Error executing {self._db_label} query")
            return f"Error executing query: {exc}"

    def _summarize_table(self, table: str) -> str:
        connection: Any | None = None
        try:
            connection = self._ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s;",
                    (self.table_schema, table),
                )
                columns = cursor.fetchall()
                if not columns:
                    return f"Error: Table '{table}' not found in schema '{self.table_schema}'."

                summary_parts = [f"Summary for table: {table}\n"]
                for col in columns:
                    col_name = str(col["column_name"] if isinstance(col, dict) else col[0])
                    data_type = str(col["data_type"] if isinstance(col, dict) else col[1])
                    lower_type = data_type.lower()
                    template: LiteralString
                    if any(token in lower_type for token in self._numeric_types):
                        template = (
                            "SELECT COUNT(*) AS total_rows, COUNT({col}) AS non_null_rows, "
                            "MIN({col}) AS min, MAX({col}) AS max, AVG({col}) AS average, "
                            "STDDEV({col}) AS std_deviation FROM {tbl};"
                        )
                    elif any(token in lower_type for token in _TEXT_TYPES):
                        template = self._text_stats_sql
                    else:
                        continue
                    cursor.execute(self._format_sql_query(template, table=table, col=col_name))
                    stats = cursor.fetchone()
                    summary_parts.append(f"\n--- Column: {col_name} (Type: {data_type}) ---")
                    if stats is None:
                        summary_parts.append("  No statistics available")
                        continue
                    items = (
                        stats.items()
                        if isinstance(stats, dict)
                        else zip([desc[0] for desc in cursor.description], stats, strict=True)
                    )
                    for key, value in items:
                        rendered = f"{value:.2f}" if isinstance(value, float) else str(value)
                        summary_parts.append(f"  {key}: {rendered}")
                return "\n".join(summary_parts)
        except Exception as exc:
            self._rollback_if_open(connection, exc)
            logger.exception(f"Error summarizing {self._db_label} table", table=table)
            return f"Error summarizing table: {exc}"

    def _inspect_db(
        self,
        action: SqlInspectAction,
        table: str | None = None,
        query: str | None = None,
    ) -> str:
        try:
            if action == "show_tables":
                return self._run_db_query(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s;",
                    (self.table_schema,),
                )
            if action == "describe_table":
                table_name = require_non_empty(table, "table")
                return self._run_db_query(
                    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s;",
                    (self.table_schema, table_name),
                )
            if action == "summarize_table":
                return self._summarize_table(require_non_empty(table, "table"))
            if action == "inspect_query":
                stmt = require_non_empty(require_non_empty(query, "query").rstrip(";"), "query")
                return self._run_db_query(f"EXPLAIN {stmt}")
        except Exception as exc:
            return f"Error in {self._inspect_tool_name} ({action}): {exc}"
        return f"Unsupported action: {action}"

    def _export_table(self, table: str, path: str) -> str:
        connection: Any | None = None
        try:
            table_name = require_non_empty(table, "table")
            target_path = Path(require_non_empty(path, "path"))
            stmt = self._format_sql_query("SELECT * FROM {tbl};", table=table_name)
            connection = self._ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute(stmt)
                if cursor.description is None:
                    return f"Error: Query returned no description for table '{table_name}'."
                columns = [desc[0] for desc in cursor.description]
                with target_path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(columns)
                    writer.writerows(row.values() if isinstance(row, dict) else row for row in cursor)
        except Exception as exc:
            self._rollback_if_open(connection, exc)
            logger.exception(f"Error exporting {self._db_label} table", table=table, path=path)
            return f"Error exporting table: {exc}"
        return f"Successfully exported table '{table_name}' to '{target_path}'."
