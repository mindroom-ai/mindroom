"""SQL tool configuration."""

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
    from agno.tools.sql import SQLTools


@register_tool_with_metadata(
    name="sql",
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
            type="url",
            required=False,
            default=None,
        ),
        ConfigField(
            name="db_engine",
            label="Db Engine",
            type="text",
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
            name="tables",
            label="Tables",
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
    docs_url="https://docs.agno.com/tools/toolkits/database/sql",
)
def sql_tools() -> type[SQLTools]:
    """Return SQL tools for database operations."""
    from agno.tools.sql import SQLTools

    return SQLTools
