"""Amazon Redshift tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.redshift import RedshiftTools


@register_tool_with_metadata(
    name="redshift",
    display_name="Amazon Redshift",
    description="Query Amazon Redshift data warehouse - list tables, run SQL, and export results",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="FaDatabase",
    icon_color="text-red-600",
    config_fields=[
        ConfigField(
            name="host",
            label="Host",
            type="text",
            required=True,
            placeholder="my-cluster.xxxx.region.redshift.amazonaws.com",
            description="Redshift cluster endpoint (falls back to REDSHIFT_HOST env var)",
        ),
        ConfigField(
            name="port",
            label="Port",
            type="number",
            required=False,
            default=5439,
        ),
        ConfigField(
            name="database",
            label="Database",
            type="text",
            required=True,
            placeholder="dev",
            description="Database name (falls back to REDSHIFT_DATABASE env var)",
        ),
        ConfigField(
            name="user",
            label="Username",
            type="text",
            required=True,
            placeholder="admin",
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
    dependencies=["redshift-connector"],
    docs_url="https://docs.agno.com/tools/toolkits/others/redshift",
)
def redshift_tools() -> type[RedshiftTools]:
    """Return Amazon Redshift tools for data warehouse operations."""
    from agno.tools.redshift import RedshiftTools

    return RedshiftTools
