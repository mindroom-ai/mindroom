"""PostgreSQL tool configuration and native implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, LiteralString

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.libpq_env import reject_ambient_libpq_env
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools._sql_cursor import SqlCursorToolkit, SqlInspectAction
from mindroom.tools.native_helpers import require_non_empty

if TYPE_CHECKING:
    from mindroom.tool_system.authoring import Toolkit


class _PostgresTools(SqlCursorToolkit):
    """Read-only PostgreSQL inspection, querying, and CSV table export."""

    _db_label = "PostgreSQL"
    _inspect_tool_name = "postgres_inspect"
    _numeric_types: tuple[str, ...] = ("integer", "numeric", "real", "double precision", "bigint", "smallint")
    _text_stats_sql: LiteralString = (
        "SELECT COUNT(*) AS total_rows, COUNT({col}) AS non_null_rows, "
        "COUNT(DISTINCT {col}) AS unique_values, AVG(LENGTH({col}::text)) AS avg_length "
        "FROM {tbl};"
    )

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
        self.db_name = db_name
        self.user = user
        self.password = password
        self.host = host
        self.port = port
        super().__init__(
            name="postgres_tools",
            tools=[self.postgres_inspect, self.run_query, self.export_table_to_path],
            table_schema=table_schema,
            connection=connection,
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
        connection.commit()
        return connection

    def _empty_result_message(self, cursor: Any, /) -> str:  # noqa: ANN401 - psycopg cursor
        return cursor.statusmessage or "Query executed successfully with no output."

    def _format_sql_query(self, template: LiteralString, *, table: str, col: str | None = None) -> Any:  # noqa: ANN401
        from psycopg import sql

        parts: dict[str, Any] = {"tbl": sql.Identifier(self.table_schema, table)}
        if col is not None:
            parts["col"] = sql.Identifier(col)
        return sql.SQL(template).format(**parts)

    def postgres_inspect(
        self,
        action: SqlInspectAction,
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
        return self._inspect_db(action, table=table, query=query)

    def run_query(self, query: str) -> str:
        """Run a read-only SQL query against PostgreSQL and return CSV-formatted rows."""
        return self._run_db_query(query)

    def export_table_to_path(self, table: str, path: str) -> str:
        """Export a PostgreSQL table as a local CSV file at `path`."""
        return self._export_table(table, path)


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
