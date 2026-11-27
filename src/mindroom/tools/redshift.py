"""Amazon Redshift tool configuration and native implementation."""

from __future__ import annotations

import csv
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from mindroom.logging_config import get_logger
from mindroom.runtime_env_policy import AWS_ENV_BY_KEY
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import (
    UNSET,
    format_tabular_rows,
    quote_sql_identifier,
    require_non_empty,
    resolve_bool_toggle,
)

logger = get_logger(__name__)

_RedshiftInspectAction = Literal["show_tables", "describe_table", "summarize_table", "inspect_query"]
_NUMERIC_TYPES = (
    "integer",
    "int",
    "bigint",
    "smallint",
    "numeric",
    "decimal",
    "real",
    "double precision",
    "float",
)
_TEXT_TYPES = ("char", "varchar", "text", "uuid")


class _RedshiftTools(Toolkit):
    """Inspect, query, and export Amazon Redshift data warehouse tables."""

    requires_connect = True

    def __init__(
        self,
        host: str | None = None,
        port: int = 5439,
        database: str | None = None,
        user: str | None = None,
        password: str | None = None,
        iam: bool | None = UNSET,
        cluster_identifier: str | None = None,
        region: str | None = None,
        db_user: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        profile: str | None = None,
        ssl: bool | None = True,
        table_schema: str = "public",
    ) -> None:
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.iam = resolve_bool_toggle(iam, default=False)
        self.cluster_identifier = cluster_identifier
        self.region = region
        self.db_user = db_user
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.session_token = session_token
        self.profile = profile
        self.ssl = ssl is not False
        self.table_schema = table_schema or "public"
        self._connection: Any | None = None

        super().__init__(
            name="redshift_tools",
            tools=[self.redshift_inspect, self.run_query, self.export_table_to_path],
        )

    def _build_connection_kwargs(self) -> dict[str, Any]:
        host = require_non_empty(self.host, "host")
        database = require_non_empty(self.database, "database")
        kwargs: dict[str, Any] = {
            "host": host,
            "port": self.port,
            "database": database,
            "ssl": self.ssl,
        }
        if self.iam:
            has_profile = isinstance(self.profile, str) and bool(self.profile.strip())
            has_keys = (
                isinstance(self.access_key_id, str)
                and bool(self.access_key_id.strip())
                and isinstance(self.secret_access_key, str)
                and bool(self.secret_access_key.strip())
            )
            if not has_profile and not has_keys:
                msg = "Redshift IAM authentication requires either profile or both access_key_id and secret_access_key"
                raise ValueError(msg)
            kwargs["iam"] = True
            if self.cluster_identifier:
                kwargs["cluster_identifier"] = self.cluster_identifier
                if self.db_user:
                    kwargs["db_user"] = self.db_user
            if self.region:
                kwargs["region"] = self.region
            if has_profile:
                kwargs["profile"] = self.profile
            else:
                kwargs["access_key_id"] = self.access_key_id
                kwargs["secret_access_key"] = self.secret_access_key
                if self.session_token:
                    kwargs["session_token"] = self.session_token
        else:
            kwargs["user"] = require_non_empty(self.user, "user")
            kwargs["password"] = require_non_empty(self.password, "password")
        return kwargs

    def _ensure_connection(self) -> Any:  # noqa: ANN401 - optional redshift_connector connection
        if self._connection is None:
            import redshift_connector

            self._connection = redshift_connector.connect(**self._build_connection_kwargs())
        return self._connection

    def connect(self) -> None:
        """Open the Redshift connection for the run."""
        self._ensure_connection()

    def close(self) -> None:
        """Close and clear the cached Redshift connection."""
        connection = self._connection
        self._connection = None
        if connection is not None:
            with suppress(Exception):
                connection.close()

    def _run_db_query(self, query: str, params: tuple[object, ...] | None = None) -> str:
        import redshift_connector

        try:
            connection = self._ensure_connection()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(query, params)
                    if cursor.description is None:
                        return "Query executed successfully."
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    if not rows:
                        return f"Query returned no results.\nColumns: {', '.join(columns)}"
                    return format_tabular_rows(columns, [tuple(row) for row in rows])
            except redshift_connector.Error:
                with suppress(Exception):
                    connection.rollback()
                raise
        except Exception as exc:
            logger.exception("Error executing Redshift query")
            return f"Error executing query: {exc}"

    def _summarize_table(self, table: str) -> str:
        import redshift_connector

        connection: Any | None = None
        try:
            full_table = f"{quote_sql_identifier(self.table_schema)}.{quote_sql_identifier(table)}"
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
                for col_name, data_type in columns:
                    col_ident = quote_sql_identifier(str(col_name))
                    lower_type = str(data_type).lower()
                    if any(token in lower_type for token in _NUMERIC_TYPES):
                        stmt = (
                            f"SELECT COUNT(*) AS total_rows, COUNT({col_ident}) AS non_null_rows, "  # noqa: S608 - quoted idents
                            f"MIN({col_ident}) AS min, MAX({col_ident}) AS max, "
                            f"AVG({col_ident}) AS average, STDDEV({col_ident}) AS std_deviation "
                            f"FROM {full_table};"
                        )
                    elif any(token in lower_type for token in _TEXT_TYPES):
                        stmt = (
                            f"SELECT COUNT(*) AS total_rows, COUNT({col_ident}) AS non_null_rows, "  # noqa: S608 - quoted idents
                            f"COUNT(DISTINCT {col_ident}) AS unique_values, AVG(LEN({col_ident})) AS avg_length "
                            f"FROM {full_table};"
                        )
                    else:
                        continue
                    cursor.execute(stmt)
                    stats = cursor.fetchone()
                    summary_parts.append(f"\n--- Column: {col_name} (Type: {data_type}) ---")
                    if stats is None:
                        summary_parts.append("  No statistics available")
                        continue
                    for key, value in zip([desc[0] for desc in cursor.description], stats, strict=True):
                        rendered = f"{value:.2f}" if isinstance(value, float) else str(value)
                        summary_parts.append(f"  {key}: {rendered}")
                return "\n".join(summary_parts)
        except redshift_connector.Error as exc:
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
            logger.exception("Error summarizing Redshift table", table=table)
            return f"Error summarizing table: {exc}"
        except Exception as exc:
            logger.exception("Error summarizing Redshift table", table=table)
            return f"Error summarizing table: {exc}"

    def redshift_inspect(
        self,
        action: _RedshiftInspectAction,
        table: str | None = None,
        query: str | None = None,
    ) -> str:
        """Inspect Redshift schema, table statistics, and query plans.

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
            return f"Error in redshift_inspect ({action}): {exc}"
        return f"Unsupported action: {action}"

    def run_query(self, query: str) -> str:
        """Run a SQL query against Amazon Redshift and return CSV-formatted rows."""
        return self._run_db_query(query)

    def export_table_to_path(self, table: str, path: str) -> str:
        """Export a Redshift table as a local CSV file at `path`."""
        import redshift_connector

        connection: Any | None = None
        try:
            table_name = require_non_empty(table, "table")
            target_path = Path(require_non_empty(path, "path"))
            full_table = f"{quote_sql_identifier(self.table_schema)}.{quote_sql_identifier(table_name)}"
            stmt = f"SELECT * FROM {full_table};"  # noqa: S608 - schema and table quoted via quote_sql_identifier
            connection = self._ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute(stmt)
                if cursor.description is None:
                    return f"Error: Query returned no description for table '{table_name}'."
                columns = [desc[0] for desc in cursor.description]
                with target_path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(columns)
                    writer.writerows(cursor)
        except redshift_connector.Error as exc:
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
            logger.exception("Error exporting Redshift table", table=table, path=path)
            return f"Error exporting table: {exc}"
        except Exception as exc:
            logger.exception("Error exporting Redshift table", table=table, path=path)
            return f"Error exporting table: {exc}"
        return f"Successfully exported table '{table_name}' to '{target_path}'."


@register_tool_with_metadata(
    name="redshift",
    file_access=ToolFileAccess.UNCONFINED,
    display_name="Amazon Redshift",
    description="Query Amazon Redshift data warehouse - list tables, run SQL, and export results",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="AwsRedshift",
    icon_color="text-red-600",
    config_fields=[
        ConfigField(
            name="host",
            env=("REDSHIFT_HOST",),
            label="Host",
            type="url",
            required=True,
            placeholder="my-cluster.xxxx.region.redshift.amazonaws.com",
            description="Redshift cluster endpoint",
        ),
        ConfigField(
            name="port",
            label="Port",
            type="number",
            required=False,
            default=5439,
        ),
        ConfigField(
            name="database",
            env=("REDSHIFT_DATABASE",),
            label="Database",
            type="text",
            required=True,
            placeholder="dev",
            description="Database name",
        ),
        ConfigField(
            name="user",
            label="Username",
            type="text",
            required=True,
            placeholder="admin",
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=True,
        ),
        bool_config_field(
            "iam",
            "Use IAM Authentication",
            default=False,
            description="Use IAM authentication instead of password",
        ),
        ConfigField(
            name="cluster_identifier",
            env=("REDSHIFT_CLUSTER_IDENTIFIER",),
            label="Cluster Identifier",
            type="text",
            required=False,
            default=None,
            description="Redshift cluster identifier (required for IAM auth)",
        ),
        ConfigField(
            name="region",
            env=(AWS_ENV_BY_KEY["region"], AWS_ENV_BY_KEY["default_region"]),
            label="Region",
            type="text",
            required=False,
            default=None,
            placeholder="us-east-1",
        ),
        ConfigField(
            name="db_user",
            env=("REDSHIFT_DB_USER",),
            label="DB User",
            type="text",
            required=False,
            default=None,
            description="Database user for IAM authentication",
        ),
        ConfigField(
            name="access_key_id",
            env=(AWS_ENV_BY_KEY["access_key"],),
            label="Access Key ID",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="secret_access_key",
            env=(AWS_ENV_BY_KEY["secret_key"],),
            label="Secret Access Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="session_token",
            env=(AWS_ENV_BY_KEY["session_token"],),
            label="Session Token",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="profile",
            env=(AWS_ENV_BY_KEY["profile"],),
            label="AWS Profile",
            type="text",
            required=False,
            default=None,
        ),
        bool_config_field("ssl", "SSL"),
        ConfigField(
            name="table_schema",
            label="Table Schema",
            type="text",
            required=False,
            default="public",
        ),
    ],
    dependencies=["redshift-connector"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#redshift",
    function_names=("export_table_to_path", "redshift_inspect", "run_query"),
    action_scoped_grants=("redshift_inspect",),
)
def redshift_tools() -> type[Toolkit]:
    """Return Amazon Redshift tools for data warehouse operations."""
    return _RedshiftTools
