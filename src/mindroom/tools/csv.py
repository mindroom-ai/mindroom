"""CSV toolkit tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.csv_toolkit import CsvTools


@register_tool_with_metadata(
    name="csv",
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
            label="Csvs",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="row_limit",
            label="Row Limit",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="duckdb_connection",
            label="Duckdb Connection",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="duckdb_kwargs",
            label="Duckdb Kwargs",
            type="text",
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
    docs_url="https://docs.agno.com/tools/toolkits/database/csv",
)
def csv_tools() -> type[CsvTools]:
    """Return CSV toolkit for data analysis and querying."""
    from agno.tools.csv_toolkit import CsvTools

    return CsvTools
