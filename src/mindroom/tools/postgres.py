"""PostgreSQL tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField
from mindroom.tools_metadata import SetupType
from mindroom.tools_metadata import ToolCategory
from mindroom.tools_metadata import ToolStatus
from mindroom.tools_metadata import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.postgres import PostgresTools


@register_tool_with_metadata(
    name="postgres",
    display_name="PostgreSQL",
    description="PostgreSQL database toolkit for querying, inspecting, and managing PostgreSQL databases",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.NONE,
    icon="FaDatabase",
    icon_color="text-blue-600",
    config_fields=[
        # Connection parameters
        ConfigField(
            name="db_name",
            label="Database Name",
            type="text",
            required=False,
            placeholder="ai",
            description="Name of the database to connect to",
        ),
        ConfigField(
            name="user",
            label="Username",
            type="text",
            required=False,
            placeholder="postgres",
            description="Username for database authentication",
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=False,
            placeholder="password",
            description="Password for database authentication",
        ),
        ConfigField(
            name="host",
            label="Host",
            type="text",
            required=False,
            default="localhost",
            placeholder="localhost",
            description="Host for the database connection",
        ),
        ConfigField(
            name="port",
            label="Port",
            type="number",
            required=False,
            default=5432,
            placeholder="5432",
            description="Port for the database connection",
        ),
        ConfigField(
            name="table_schema",
            label="Table Schema",
            type="text",
            required=False,
            default="public",
            placeholder="public",
            description="Default schema to use for database operations",
        ),
        # Feature flags
        ConfigField(
            name="run_queries",
            label="Run Queries",
            type="boolean",
            required=False,
            default=True,
            description="Enable running SQL queries",
        ),
        ConfigField(
            name="inspect_queries",
            label="Inspect Queries",
            type="boolean",
            required=False,
            default=False,
            description="Enable inspecting SQL queries before execution",
        ),
        ConfigField(
            name="summarize_tables",
            label="Summarize Tables",
            type="boolean",
            required=False,
            default=True,
            description="Enable summarizing table structures",
        ),
        ConfigField(
            name="export_tables",
            label="Export Tables",
            type="boolean",
            required=False,
            default=False,
            description="Enable exporting tables from the database",
        ),
    ],
    dependencies=["psycopg-binary"],
    docs_url="https://docs.agno.com/tools/toolkits/database/postgres",
)
def postgres_tools() -> type[PostgresTools]:
    """Return PostgreSQL tools for database management."""
    from agno.tools.postgres import PostgresTools

    return PostgresTools
