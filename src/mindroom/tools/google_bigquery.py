"""Google BigQuery tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.google_bigquery import GoogleBigQueryTools


@register_tool_with_metadata(
    name="google_bigquery",
    file_access=ToolFileAccess.NONE,
    display_name="Google BigQuery",
    description="Query Google BigQuery - list tables, describe schemas, and run SQL queries",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="google_cloud",
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
            description="Google Cloud project ID",
        ),
        ConfigField(
            name="location",
            label="Location",
            type="text",
            required=True,
            placeholder="US",
            description="BigQuery location",
        ),
        ConfigField(
            name="max_rows",
            label="Max Rows",
            type="number",
            required=False,
            default=100,
            description="Maximum rows returned by run_sql_query (1 to 1000)",
            validation={"min": 1, "max": 1000},
        ),
        bool_config_field("list_tables", "List Tables"),
        bool_config_field("describe_table", "Describe Table"),
        bool_config_field("run_sql_query", "Run SQL Query"),
        bool_config_field("all", "All", default=False),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    dependencies=[
        "google-cloud-bigquery",
        "google-api-python-client",
        "google-auth",
        "google-auth-httplib2",
    ],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#google_bigquery",
    helper_text="Connect Google Cloud, then set the project, dataset, and location. Queries run as the connected account with read-only access.",
    function_names=("google_bigquery_inspect", "run_sql_query"),
    action_scoped_grants=("google_bigquery_inspect",),
)
def google_bigquery_tools() -> type[GoogleBigQueryTools]:
    """Return Google BigQuery tools for data analytics."""
    from mindroom.custom_tools.google_bigquery import GoogleBigQueryTools

    return GoogleBigQueryTools
