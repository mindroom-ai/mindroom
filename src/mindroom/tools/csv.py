"""CSV toolkit tool configuration and native implementation."""

from __future__ import annotations

import csv
import itertools
import json
from contextlib import nullcontext, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import (
    UNSET,
    first_sql_statement,
    format_tabular_rows,
    narrow_action_enum,
    require_enabled_action,
    resolve_bool_toggle,
)

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_CsvInspectAction = Literal["list", "columns", "read"]


class _CsvTools(Toolkit):
    """Inspect and query configured CSV files by filename stem."""

    def __init__(
        self,
        csvs: list[str | Path] | None = None,
        row_limit: int | None = None,
        duckdb_connection: Any | None = None,  # noqa: ANN401 - optional caller-supplied DuckDB connection
        duckdb_kwargs: dict[str, Any] | None = None,
        enable_read_csv_file: bool | None = UNSET,
        enable_list_csv_files: bool | None = UNSET,
        enable_get_columns: bool | None = UNSET,
        enable_query_csv_file: bool | None = UNSET,
        all: bool | None = UNSET,  # noqa: A002 - matches ConfigField name
    ) -> None:
        self.csvs: list[Path] = []
        if csvs:
            for item in csvs:
                if isinstance(item, (str, Path)):
                    self.csvs.append(Path(item))
                else:
                    msg = f"Invalid csv file: {item}"
                    raise TypeError(msg)
        self.row_limit = row_limit
        self.duckdb_connection = duckdb_connection
        self.duckdb_kwargs = duckdb_kwargs

        enable_all = resolve_bool_toggle(all, default=False)
        inspect_actions: list[str] = []
        if enable_all or resolve_bool_toggle(enable_list_csv_files, default=True):
            inspect_actions.append("list")
        if enable_all or resolve_bool_toggle(enable_get_columns, default=True):
            inspect_actions.append("columns")
        if enable_all or resolve_bool_toggle(enable_read_csv_file, default=True):
            inspect_actions.append("read")
        self._inspect_actions = tuple(inspect_actions)

        tools: list[Any] = []
        if self._inspect_actions:
            tools.append(self.csv_inspect)
        if enable_all or resolve_bool_toggle(enable_query_csv_file, default=True):
            tools.append(self.query_csv_file)

        super().__init__(name="csv_tools", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict ``csv_inspect`` actions to the ones enabled on this instance."""
        if function.name == "csv_inspect":
            return narrow_action_enum(function, self._inspect_actions)
        return function

    def _resolve_csv(self, csv_name: str | None) -> Path | None:
        if not csv_name:
            return None
        for path in self.csvs:
            if path.stem == csv_name:
                return path
        return None

    def _missing_csv_message(self, csv_name: str | None) -> str:
        available = json.dumps([path.stem for path in self.csvs])
        return f"File: {csv_name} not found, please use one of {available}"

    def csv_inspect(
        self,
        action: _CsvInspectAction,
        csv_name: str | None = None,
        row_limit: int | None = None,
    ) -> str:
        """Inspect configured CSV files by filename stem.

        Actions:
        - `list`: list available CSV names (stems).
        - `columns`: return column names for `csv_name`.
        - `read`: return rows of `csv_name` as JSON, capped by `row_limit` or configured default.
        """
        if (disabled := require_enabled_action(action, self._inspect_actions)) is not None:
            return disabled
        if action == "list":
            return json.dumps([path.stem for path in self.csvs])

        file_path = self._resolve_csv(csv_name)
        if file_path is None:
            return self._missing_csv_message(csv_name)

        try:
            with file_path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if action == "columns":
                    return json.dumps(reader.fieldnames)
                effective_limit = self.row_limit if row_limit is None else row_limit
                rows = (
                    list(itertools.islice(reader, max(0, effective_limit)))
                    if effective_limit is not None
                    else list(reader)
                )
            return json.dumps(rows)
        except Exception as exc:
            logger.exception("Error inspecting CSV", csv_name=csv_name, action=action)
            return f"Error reading csv: {exc}"

    def query_csv_file(self, csv_name: str, sql_query: str) -> str:
        """Run a single DuckDB SQL query against configured CSV `csv_name` (table name equals `csv_name`)."""
        file_path = self._resolve_csv(csv_name)
        if file_path is None:
            return self._missing_csv_message(csv_name)

        try:
            import duckdb
        except ImportError:
            return "Error querying csv: `duckdb` is not installed."

        try:
            conn_ctx = (
                nullcontext(self.duckdb_connection)
                if self.duckdb_connection is not None
                else duckdb.connect(**(self.duckdb_kwargs or {}))
            )
            with conn_ctx as con:
                table_ident = '"' + csv_name.replace('"', '""') + '"'
                stmt = (
                    f"CREATE TEMP TABLE {table_ident} AS "  # noqa: S608 - single-part quoted identifier
                    "SELECT * FROM read_csv(?, ignore_errors=false, auto_detect=true)"
                )
                created_temp = False
                try:
                    con.execute(stmt, [str(file_path)])
                    created_temp = True
                    formatted_sql = first_sql_statement(sql_query)
                    query_result = con.sql(formatted_sql)
                    if query_result is None:
                        return "No output"
                    try:
                        rows = query_result.fetchall()
                        return format_tabular_rows(query_result.columns, rows)
                    except AttributeError:
                        return str(query_result)
                finally:
                    if created_temp:
                        with suppress(Exception):
                            con.execute(f"DROP TABLE IF EXISTS temp.main.{table_ident}")
        except Exception as exc:
            logger.exception("Error querying CSV", csv_name=csv_name)
            return f"Error querying csv: {exc}"


@register_tool_with_metadata(
    name="csv",
    file_access=ToolFileAccess.UNCONFINED,
    display_name="CSV Toolkit",
    description="CSV file analysis and querying with SQL support",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaFileCsv",
    icon_color="text-green-600",
    config_fields=[
        ConfigField(
            name="csvs",
            label="CSV Files",
            type="string[]",
            required=False,
            default=None,
            description="Paths of the CSV files the tool reads, each named by its filename stem.",
        ),
        ConfigField(
            name="row_limit",
            label="Row Limit",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_read_csv_file",
            label="Enable Read Csv File",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_list_csv_files",
            label="Enable List Csv Files",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_columns",
            label="Enable Get Columns",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_query_csv_file",
            label="Enable Query Csv File",
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
    dependencies=["duckdb"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#csv",
    function_names=("csv_inspect", "query_csv_file"),
    action_scoped_grants=("csv_inspect",),
)
def csv_tools() -> type[Toolkit]:
    """Return CSV toolkit for data analysis and querying."""
    return _CsvTools
