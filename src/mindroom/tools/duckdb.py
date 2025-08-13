"""DuckDB tool configuration."""

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
    from agno.tools.duckdb import DuckDbTools


@register_tool_with_metadata(
    name="duckdb",
    display_name="DuckDB",
    description="In-memory analytical database for data processing and analysis",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Database",
    icon_color="text-yellow-600",
    config_fields=[
        # Database connection parameters
        ConfigField(
            name="db_path",
            label="Database Path",
            type="text",
            required=False,
            default=None,
            placeholder="/path/to/database.db",
            description="Path to the DuckDB database file (if None, uses in-memory database)",
        ),
        ConfigField(
            name="connection",
            label="Database Connection",
            type="text",
            required=False,
            default=None,
            placeholder="Optional existing DuckDB connection",
            description="Existing DuckDB connection object (advanced users only)",
        ),
        ConfigField(
            name="init_commands",
            label="Initialization Commands",
            type="text",
            required=False,
            default=None,
            placeholder='["INSTALL spatial", "LOAD spatial"]',
            description="List of SQL commands to run on initialization (as JSON array)",
        ),
        ConfigField(
            name="read_only",
            label="Read Only",
            type="boolean",
            required=False,
            default=False,
            description="Open database in read-only mode",
        ),
        ConfigField(
            name="config",
            label="Database Config",
            type="text",
            required=False,
            default=None,
            placeholder='{"memory_limit": "1GB"}',
            description="Database configuration as JSON string (optional)",
        ),
        # Query operations
        ConfigField(
            name="run_queries",
            label="Run Queries",
            type="boolean",
            required=False,
            default=True,
            description="Enable executing SQL queries",
        ),
        ConfigField(
            name="inspect_queries",
            label="Inspect Queries",
            type="boolean",
            required=False,
            default=False,
            description="Enable query plan inspection and analysis",
        ),
        # Table operations
        ConfigField(
            name="create_tables",
            label="Create Tables",
            type="boolean",
            required=False,
            default=True,
            description="Enable creating tables from files and data sources",
        ),
        ConfigField(
            name="summarize_tables",
            label="Summarize Tables",
            type="boolean",
            required=False,
            default=True,
            description="Enable table summarization with statistical analysis",
        ),
        ConfigField(
            name="export_tables",
            label="Export Tables",
            type="boolean",
            required=False,
            default=False,
            description="Enable exporting tables to various file formats",
        ),
    ],
    dependencies=["duckdb"],
    docs_url="https://docs.agno.com/tools/toolkits/database/duckdb",
)
def duckdb_tools() -> type[DuckDbTools]:
    """Return DuckDB tools for data analysis and processing."""
    from agno.tools.duckdb import DuckDbTools

    return DuckDbTools
