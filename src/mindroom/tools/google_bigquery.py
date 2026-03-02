"""Google BigQuery tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.google_bigquery import GoogleBigQueryTools


@register_tool_with_metadata(
    name="google_bigquery",
    display_name="Google BigQuery",
    description="Query Google BigQuery - list tables, describe schemas, and run SQL queries",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="SiGooglebigquery",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="dataset",
            label="Dataset",
            type="text",
            required=True,
            placeholder="my_dataset",
            description="BigQuery dataset name",
        ),
        ConfigField(
            name="project",
            label="Project",
            type="text",
            required=True,
            placeholder="my-gcp-project",
            description="Google Cloud project ID (falls back to GOOGLE_CLOUD_PROJECT env var)",
        ),
        ConfigField(
            name="location",
            label="Location",
            type="text",
            required=True,
            placeholder="US",
            description="BigQuery location (falls back to GOOGLE_CLOUD_LOCATION env var)",
        ),
        ConfigField(
            name="credentials",
            label="Credentials",
            type="text",
            required=False,
            default=None,
            description="Google Cloud credentials object (optional, uses Application Default Credentials if not set)",
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
            label="Enable Run SQL Query",
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
    dependencies=["google-cloud-bigquery"],
    docs_url="https://docs.agno.com/tools/toolkits/others/google_bigquery",
    helper_text="Requires Google Cloud credentials. Set up [Application Default Credentials](https://cloud.google.com/docs/authentication/provide-credentials-adc)",
)
def google_bigquery_tools() -> type[GoogleBigQueryTools]:
    """Return Google BigQuery tools for data analytics."""
    from agno.tools.google_bigquery import GoogleBigQueryTools

    return GoogleBigQueryTools
