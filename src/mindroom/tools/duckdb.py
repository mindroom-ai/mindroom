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
        ConfigField(
            name="db_path",
            label="Db Path",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="connection",
            label="Connection",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="init_commands",
            label="Init Commands",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="read_only",
            label="Read Only",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="config",
            label="Config",
            type="text",
            required=False,
            default=None,
        ),
    ],
    dependencies=["duckdb"],
    docs_url="https://docs.agno.com/tools/toolkits/database/duckdb",
)
def duckdb_tools() -> type[DuckDbTools]:
    """Return DuckDB tools for data analysis and processing."""
    from agno.tools.duckdb import DuckDbTools

    return DuckDbTools
