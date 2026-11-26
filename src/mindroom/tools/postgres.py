"""PostgreSQL tool configuration and native implementation."""

from __future__ import annotations

import csv
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.libpq_env import reject_ambient_libpq_env
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import format_tabular_rows, require_non_empty

logger = get_logger(__name__)

_PostgresInspectAction = Literal["show_tables", "describe_table", "summarize_table", "inspect_query"]
_NUMERIC_TYPES = ("integer", "numeric", "real", "double precision", "bigint", "smallint")
_TEXT_TYPES = ("char", "text", "uuid")


class _PostgresTools(Toolkit):
    """Read-only PostgreSQL inspection, querying, and CSV table export."""

    requires_connect = True

    def __init__(
        self,
        connection: Any | None = None,  # noqa: ANN401 - optional caller-supplied psycopg connection
        db_name: str | None = None,
        user: str | None = None,
        password: str | None = None,
        host: str | None = None,
        port: int | None = 5432,
        table_schema: str = "public",
    ) -> None:
        self._connection: Any | None = connection
        self._owns_connection: bool = False
        self.db_name = db_name
        self.user = user
        self.password = password
        self.host = host
        self.port = port
        self.table_schema = table_schema or "public"

        super().__init__(
            name="postgres_tools",
            tools=[self.postgres_inspect, self.run_query, self.export_table_to_path],
        )

    def _open_connection(self) -> Any:  # noqa: ANN401 - psycopg.Connection is an optional runtime import
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row

        missing = [
            name
            for name, value in (
                ("host", self.host),
                ("db_name", self.db_name),
                ("user", self.user),
                ("password", self.password),
            )
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            msg = f"PostgreSQL connection requires configured credentials: {', '.join(missing)}"
            raise ValueError(msg)

        schema = require_non_empty(self.table_schema, "table_schema")
        if "\x00" in schema:
            msg = "table_schema must not contain NUL bytes"
            raise ValueError(msg)

        reject_ambient_libpq_env()
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": 5432 if self.port is None else self.port,
            "dbname": self.db_name,
            "user": self.user,
            "password": self.password,
            "passfile": "",
            "row_factory": dict_row,
        }
        connection = psycopg.connect(**kwargs)
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    def _ensure_connection(self) -> Any:  # noqa: ANN401 - optional psycopg connection
        if self._connection is None or getattr(self._connection, "closed", False):
            self._connection = self._open_connection()
            self._owns_connection = True
        return self._connection

    def connect(self) -> None:
        """Open the PostgreSQL connection for the run."""
        self._ensure_connection()

    def close(self) -> None:
        """Close and clear a toolkit-owned PostgreSQL connection."""
        connection = self._connection
        owns = self._owns_connection
        self._connection = None
        self._owns_connection = False
        if owns and connection is not None and not getattr(connection, "closed", False):
            with suppress(Exception):
                connection.close()

    def _run_db_query(self, query: str, params: tuple[object, ...] | None = None) -> str:
        try:
            connection = self._ensure_connection()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(query, params)
                    if cursor.description is None:
                        return cursor.statusmessage or "Query executed successfully with no output."
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    if not rows:
                        return f"Query returned no results.\nColumns: {', '.join(columns)}"
                    row_values = [tuple(row.values()) if isinstance(row, dict) else tuple(row) for row in rows]
                    return format_tabular_rows(columns, row_values)
            except Exception:
                if not getattr(connection, "closed", False):
                    with suppress(Exception):
                        connection.rollback()
                raise
        except Exception as exc:
            logger.exception("Error executing PostgreSQL query")
            return f"Error executing query: {exc}"

    def _summarize_table(self, table: str) -> str:
        from psycopg import sql

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
                table_ident = sql.Identifier(self.table_schema, table)
                for col in columns:
                    col_name = col["column_name"] if isinstance(col, dict) else col[0]
                    data_type = col["data_type"] if isinstance(col, dict) else col[1]
                    col_ident = sql.Identifier(col_name)
                    if any(token in data_type for token in _NUMERIC_TYPES):
                        stats_query = sql.SQL(
                            "SELECT COUNT(*) AS total_rows, COUNT({col}) AS non_null_rows, "
                            "MIN({col}) AS min, MAX({col}) AS max, AVG({col}) AS average, "
                            "STDDEV({col}) AS std_deviation FROM {tbl};",
                        ).format(col=col_ident, tbl=table_ident)
                    elif any(token in data_type for token in _TEXT_TYPES):
                        stats_query = sql.SQL(
                            "SELECT COUNT(*) AS total_rows, COUNT({col}) AS non_null_rows, "
                            "COUNT(DISTINCT {col}) AS unique_values, AVG(LENGTH({col}::text)) AS avg_length "
                            "FROM {tbl};",
                        ).format(col=col_ident, tbl=table_ident)
                    else:
                        continue
                    cursor.execute(stats_query)
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
            if connection is not None and not getattr(connection, "closed", False):
                with suppress(Exception):
                    connection.rollback()
            logger.exception("Error summarizing PostgreSQL table", table=table)
            return f"Error summarizing table: {exc}"

    def postgres_inspect(
        self,
        action: _PostgresInspectAction,
        table: str | None = None,
        query: str | None = None,
    ) -> str:
        """Inspect PostgreSQL schema, table statistics, and query plans.

        Actions:
        - `show_tables`: list tables in the configured schema.
        - `describe_table`: show column names, types, and nullability for `table`.
        - `summarize_table`: compute per-column statistics for `table`.
        - `inspect_query`: return `EXPLAIN` execution plan for `query`.
        """
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
            return f"Error in postgres_inspect ({action}): {exc}"
        return f"Unsupported action: {action}"

    def run_query(self, query: str) -> str:
        """Run a read-only SQL query against PostgreSQL and return CSV-formatted rows."""
        return self._run_db_query(query)

    def export_table_to_path(self, table: str, path: str) -> str:
        """Export a PostgreSQL table as a local CSV file at `path`."""
        connection: Any | None = None
        try:
            from psycopg import sql

            table_name = require_non_empty(table, "table")
            target_path = Path(require_non_empty(path, "path"))
            stmt = sql.SQL("SELECT * FROM {tbl};").format(tbl=sql.Identifier(self.table_schema, table_name))
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
            if connection is not None and not getattr(connection, "closed", False):
                with suppress(Exception):
                    connection.rollback()
            logger.exception("Error exporting PostgreSQL table", table=table, path=path)
            return f"Error exporting table: {exc}"
        return f"Successfully exported table '{table_name}' to '{target_path}'."


@register_tool_with_metadata(
    name="postgres",
    file_access=ToolFileAccess.UNCONFINED,
    display_name="PostgreSQL",
    description="Query PostgreSQL databases - list tables, describe schemas, run SQL queries, and export data",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="SiPostgresql",
    icon_color="text-blue-700",
    config_fields=[
        ConfigField(
            name="host",
            label="Host",
            type="url",
            required=True,
            placeholder="localhost",
            description="PostgreSQL server hostname",
        ),
        ConfigField(
            name="port",
            label="Port",
            type="number",
            required=False,
            default=5432,
        ),
        ConfigField(
            name="db_name",
            label="Database Name",
            type="text",
            required=True,
            placeholder="mydb",
        ),
        ConfigField(
            name="user",
            label="Username",
            type="text",
            required=True,
            placeholder="postgres",
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=True,
        ),
        ConfigField(
            name="table_schema",
            label="Table Schema",
            type="text",
            required=False,
            default="public",
        ),
    ],
    dependencies=["psycopg"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#postgres",
    function_names=("export_table_to_path", "postgres_inspect", "run_query"),
    action_scoped_grants=("postgres_inspect",),
)
def postgres_tools() -> type[Toolkit]:
    """Return PostgreSQL tools for database operations."""
    return _PostgresTools
