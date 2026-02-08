"""PostgreSQL tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.postgres import PostgresTools


@register_tool_with_metadata(
    name="postgres",
    display_name="PostgreSQL",
    description="Query PostgreSQL databases - list tables, describe schemas, run SQL queries, and export data",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="FaDatabase",
    icon_color="text-blue-700",
    config_fields=[
        ConfigField(
            name="host",
            label="Host",
            type="text",
            required=True,
            placeholder="localhost",
            description="PostgreSQL server hostname",
        ),
        ConfigField(
            name="port",
            label="Port",
            type="number",
            required=False,
            default=5432,
        ),
        ConfigField(
            name="db_name",
            label="Database Name",
            type="text",
            required=True,
            placeholder="mydb",
        ),
        ConfigField(
            name="user",
            label="Username",
            type="text",
            required=True,
            placeholder="postgres",
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=True,
        ),
        ConfigField(
            name="table_schema",
            label="Table Schema",
            type="text",
            required=False,
            default="public",
        ),
    ],
    dependencies=["psycopg-binary"],
    docs_url="https://docs.agno.com/tools/toolkits/others/postgres",
)
def postgres_tools() -> type[PostgresTools]:
    """Return PostgreSQL tools for database operations."""
    from agno.tools.postgres import PostgresTools

    return PostgresTools
