"""CSV toolkit tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
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
        # File configuration
        ConfigField(
            name="csvs",
            label="CSV Files",
            type="text",
            required=False,
            placeholder="/path/to/file1.csv,/path/to/file2.csv",
            description="List of CSV file paths to work with (comma-separated)",
        ),
        ConfigField(
            name="row_limit",
            label="Row Limit",
            type="number",
            required=False,
            placeholder="1000",
            description="Maximum number of rows to read from CSV files",
        ),
        # Feature toggles
        ConfigField(
            name="read_csvs",
            label="Read CSVs",
            type="boolean",
            required=False,
            default=True,
            description="Enable reading CSV file contents",
        ),
        ConfigField(
            name="list_csvs",
            label="List CSVs",
            type="boolean",
            required=False,
            default=True,
            description="Enable listing available CSV files",
        ),
        ConfigField(
            name="query_csvs",
            label="Query CSVs",
            type="boolean",
            required=False,
            default=True,
            description="Enable SQL querying of CSV files (requires DuckDB)",
        ),
        ConfigField(
            name="read_column_names",
            label="Read Column Names",
            type="boolean",
            required=False,
            default=True,
            description="Enable reading column names from CSV files",
        ),
        # DuckDB configuration
        ConfigField(
            name="duckdb_connection",
            label="DuckDB Connection",
            type="text",
            required=False,
            placeholder="Auto-created if not provided",
            description="Existing DuckDB connection object (advanced usage)",
        ),
        ConfigField(
            name="duckdb_kwargs",
            label="DuckDB Arguments",
            type="text",
            required=False,
            placeholder='{"memory_limit": "1GB"}',
            description="Additional arguments for DuckDB connection (JSON format)",
        ),
    ],
    dependencies=["duckdb"],
    docs_url="https://docs.agno.com/tools/toolkits/database/csv",
)
def csv_tools() -> type[CsvTools]:
    """Return CSV toolkit for data analysis and querying."""
    from agno.tools.csv_toolkit import CsvTools

    return CsvTools
