"""Redshift tool configuration and native implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, LiteralString

from mindroom.runtime_env_policy import AWS_ENV_BY_KEY
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools._sql_cursor import SqlCursorToolkit, SqlInspectAction
from mindroom.tools.native_helpers import UNSET, quote_sql_identifier, require_non_empty, resolve_bool_toggle

if TYPE_CHECKING:
    from mindroom.tool_system.authoring import Toolkit


class _RedshiftTools(SqlCursorToolkit):
    """Inspect, query, and export Amazon Redshift data warehouse tables."""

    _db_label = "Redshift"
    _inspect_tool_name = "redshift_inspect"
    _text_stats_sql: LiteralString = (
        "SELECT COUNT(*) AS total_rows, COUNT({col}) AS non_null_rows, "
        "COUNT(DISTINCT {col}) AS unique_values, AVG(LEN({col})) AS avg_length "
        "FROM {tbl};"
    )

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
        super().__init__(
            name="redshift_tools",
            tools=[self.redshift_inspect, self.run_query, self.export_table_to_path],
            table_schema=table_schema,
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

    def _open_connection(self) -> Any:  # noqa: ANN401 - redshift_connector is an optional runtime import
        import redshift_connector

        return redshift_connector.connect(**self._build_connection_kwargs())

    def _format_sql_query(self, template: LiteralString, *, table: str, col: str | None = None) -> str:
        parts = {"tbl": f"{quote_sql_identifier(self.table_schema)}.{quote_sql_identifier(table)}"}
        if col is not None:
            parts["col"] = quote_sql_identifier(col)
        return template.format(**parts)

    def redshift_inspect(
        self,
        action: SqlInspectAction,
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
        return self._inspect_db(action, table=table, query=query)

    def run_query(self, query: str) -> str:
        """Run a SQL query against Amazon Redshift and return CSV-formatted rows."""
        return self._run_db_query(query)

    def export_table_to_path(self, table: str, path: str) -> str:
        """Export a Redshift table as a local CSV file at `path`."""
        return self._export_table(table, path)


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
