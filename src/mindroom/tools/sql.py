"""SQL tool configuration and native implementation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.libpq_env import reject_ambient_libpq_env
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import (
    UNSET,
    narrow_action_enum,
    require_enabled_action,
    require_non_empty,
    resolve_bool_toggle,
)

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_SqlInspectAction = Literal["list_tables", "describe_table"]
_POSTGRES_BACKENDS = frozenset({"postgresql", "postgres"})
_LIBPQ_DRIVERS = frozenset({"", "psycopg", "psycopg2", "psycopg2cffi"})


def _first_query_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], str):
        return value[0]
    return None


def _prepare_engine_target(url: Any) -> tuple[Any, dict[str, Any]]:  # noqa: ANN401 - optional sqlalchemy URL
    """Enforce scoped PostgreSQL credentials and disable ambient libpq auth fallbacks."""
    backend = url.get_backend_name().lower()
    if backend not in _POSTGRES_BACKENDS:
        return url, {}

    query_lower = {key.lower(): value for key, value in url.query.items()}
    effective_host = url.host or _first_query_str(query_lower.get("host"))
    missing = [
        name
        for name, value in (
            ("host", effective_host),
            ("username", url.username),
            ("password", url.password),
            ("database", url.database),
        )
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        msg = f"PostgreSQL connection requires explicit credentials/target in URL or config: {', '.join(missing)}"
        raise ValueError(msg)

    if "service" in query_lower:
        msg = "PostgreSQL connection URL must not use libpq 'service' lookup"
        raise ValueError(msg)
    if "passfile" in query_lower and _first_query_str(query_lower["passfile"]) != "":
        msg = "PostgreSQL connection URL must not use libpq 'passfile' lookup"
        raise ValueError(msg)

    driver = url.drivername.partition("+")[2].lower()
    if driver in _LIBPQ_DRIVERS:
        has_hostaddr = bool((_first_query_str(query_lower.get("hostaddr")) or "").strip())
        reject_ambient_libpq_env(allow_hostaddr=has_hostaddr)
        connect_args: dict[str, Any] = {"passfile": ""}
        if url.port is None and not (_first_query_str(query_lower.get("port")) or "").strip():
            if url.host:
                url = url.set(port=5432)
            else:
                connect_args["port"] = 5432
        return url, connect_args
    return url, {}


class _SQLTools(Toolkit):
    """Inspect schemas and execute SQL queries through SQLAlchemy."""

    def __init__(
        self,
        db_url: str | None = None,
        db_engine: Any | None = None,  # noqa: ANN401 - optional caller-supplied SQLAlchemy Engine
        user: str | None = None,
        password: str | None = None,
        host: str | None = None,
        port: int | None = None,
        schema: str | None = None,
        dialect: str | None = None,
        tables: dict[str, Any] | None = None,
        enable_list_tables: bool | None = UNSET,
        enable_describe_table: bool | None = UNSET,
        enable_run_sql_query: bool | None = UNSET,
        all: bool | None = UNSET,  # noqa: A002 - matches ConfigField name
    ) -> None:
        from sqlalchemy import URL, create_engine
        from sqlalchemy.engine import make_url
        from sqlalchemy.orm import sessionmaker

        engine = db_engine
        if engine is None:
            target_url: URL | None = None
            if db_url:
                target_url = make_url(db_url)
            elif user and password and host and port and dialect:
                target_url = URL.create(
                    drivername=dialect,
                    username=user,
                    password=password,
                    host=host,
                    port=port,
                    database=schema,
                )
            if target_url is not None:
                prepared_url, connect_args = _prepare_engine_target(target_url)
                engine = (
                    create_engine(prepared_url, connect_args=connect_args)
                    if connect_args
                    else create_engine(prepared_url)
                )
        if engine is None:
            msg = "Could not build the database connection"
            raise ValueError(msg)

        self.db_engine: Any = engine
        self._session_factory: Any = sessionmaker(bind=self.db_engine)
        self.schema = schema
        self.tables = tables

        enable_all = resolve_bool_toggle(all, default=False)
        inspect_actions: list[str] = []
        if enable_all or resolve_bool_toggle(enable_list_tables, default=True):
            inspect_actions.append("list_tables")
        if enable_all or resolve_bool_toggle(enable_describe_table, default=True):
            inspect_actions.append("describe_table")
        self._inspect_actions = tuple(inspect_actions)

        tools: list[Any] = []
        if self._inspect_actions:
            tools.append(self.sql_inspect)
        if enable_all or resolve_bool_toggle(enable_run_sql_query, default=True):
            tools.append(self.run_sql_query)

        super().__init__(name="sql_tools", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict ``sql_inspect`` actions to the ones enabled on this instance."""
        if function.name == "sql_inspect":
            return narrow_action_enum(function, self._inspect_actions)
        return function

    def sql_inspect(
        self,
        action: _SqlInspectAction,
        table_name: str | None = None,
    ) -> str:
        """Inspect database tables and schemas via SQLAlchemy.

        Actions:
        - `list_tables`: list available table names in the configured schema.
        - `describe_table`: return column names, types, nullability, and defaults for `table_name`.
        """
        if (disabled := require_enabled_action(action, self._inspect_actions)) is not None:
            return disabled

        if action == "list_tables":
            if self.tables is not None:
                return json.dumps(self.tables)
            try:
                from sqlalchemy.inspection import inspect

                inspector = inspect(self.db_engine)
                table_names = (
                    inspector.get_table_names(schema=self.schema) if self.schema else inspector.get_table_names()
                )
                return json.dumps(table_names)
            except Exception as exc:
                logger.exception("Error listing SQL tables")
                return f"Error getting tables: {exc}"

        try:
            from sqlalchemy.inspection import inspect

            target = require_non_empty(table_name, "table_name")
            inspector = inspect(self.db_engine)
            columns = inspector.get_columns(target, schema=self.schema)
            payload = [
                {
                    "name": column["name"],
                    "type": str(column["type"]),
                    "nullable": column["nullable"],
                    "default": column.get("default"),
                }
                for column in columns
            ]
            return json.dumps(payload)
        except Exception as exc:
            logger.exception("Error describing SQL table", table_name=table_name)
            return f"Error getting table schema: {exc}"

    def run_sql_query(self, query: str, limit: int | None = 10) -> str:
        """Run a SQL query and return JSON rows (up to `limit`, or all rows when `limit` is null; non-positive returns empty)."""
        try:
            from sqlalchemy.sql.expression import text

            with self._session_factory() as sess, sess.begin():
                result = sess.execute(text(query))
                if not result.returns_rows:
                    return json.dumps([])
                if limit is None:
                    rows = result.fetchall()
                elif limit > 0:
                    rows = result.fetchmany(limit)
                else:
                    rows = []
                return json.dumps([row._asdict() for row in rows], default=str)
        except Exception as exc:
            logger.exception("Error running SQL query")
            return f"Error running query: {exc}"


@register_tool_with_metadata(
    name="sql",
    file_access=ToolFileAccess.UNCONFINED,
    requires_primary_runtime=True,
    display_name="SQL Tools",
    description="Database query and management tools for SQL databases",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="Database",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="db_url",
            label="Db URL",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="user",
            label="User",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="host",
            label="Host",
            type="url",
            required=False,
            default=None,
        ),
        ConfigField(
            name="port",
            label="Port",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="schema",
            label="Schema",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="dialect",
            label="Dialect",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_list_tables",
            label="Enable List Tables",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_describe_table",
            label="Enable Describe Table",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_run_sql_query",
            label="Enable Run Sql Query",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["sqlalchemy"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#sql",
    function_names=("run_sql_query", "sql_inspect"),
    action_scoped_grants=("sql_inspect",),
)
def sql_tools() -> type[Toolkit]:
    """Return SQL tools for database operations."""
    return _SQLTools
