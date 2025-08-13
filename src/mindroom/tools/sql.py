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
        # Database Connection - Primary method: Database URL
        ConfigField(
            name="db_url",
            label="Database URL",
            type="url",
            required=False,
            placeholder="postgresql://user:password@host:port/database",
            description="Complete database connection URL (can also be set via DB_URL env var)",
        ),
        # Advanced: Pre-configured database engine
        ConfigField(
            name="db_engine",
            label="Database Engine",
            type="text",
            required=False,
            placeholder="Advanced: Pre-configured SQLAlchemy Engine object",
            description="Pre-configured SQLAlchemy Engine instance (for advanced users)",
        ),
        # Alternative connection parameters
        ConfigField(
            name="user",
            label="Username",
            type="text",
            required=False,
            placeholder="database_user",
            description="Database username (alternative to db_url)",
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=False,
            placeholder="database_password",
            description="Database password (alternative to db_url)",
        ),
        ConfigField(
            name="host",
            label="Host",
            type="url",
            required=False,
            placeholder="localhost",
            description="Database host (alternative to db_url)",
        ),
        ConfigField(
            name="port",
            label="Port",
            type="number",
            required=False,
            placeholder="5432",
            description="Database port (alternative to db_url)",
        ),
        ConfigField(
            name="schema",
            label="Schema/Database",
            type="text",
            required=False,
            placeholder="mydb",
            description="Database schema or database name",
        ),
        ConfigField(
            name="dialect",
            label="Database Dialect",
            type="text",
            required=False,
            placeholder="postgresql",
            description="Database dialect (postgresql, mysql, sqlite, etc.)",
        ),
        # Table configuration
        ConfigField(
            name="tables",
            label="Tables Configuration",
            type="text",
            required=False,
            placeholder='{"users": {...}, "orders": {...}}',
            description="JSON configuration of specific tables to access (optional)",
        ),
        # SQL Operation Controls
        ConfigField(
            name="list_tables",
            label="List Tables",
            type="boolean",
            required=False,
            default=True,
            description="Enable listing database tables",
        ),
        ConfigField(
            name="describe_table",
            label="Describe Table",
            type="boolean",
            required=False,
            default=True,
            description="Enable describing table schemas",
        ),
        ConfigField(
            name="run_sql_query",
            label="Run SQL Query",
            type="boolean",
            required=False,
            default=True,
            description="Enable executing SQL queries",
        ),
    ],
    dependencies=["sqlalchemy"],
    docs_url="https://docs.agno.com/tools/toolkits/database/sql",
)
def sql_tools() -> type[SQLTools]:
    """Return SQL tools for database operations."""
    from agno.tools.sql import SQLTools

    return SQLTools
