"""Amazon Redshift tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.redshift import RedshiftTools


@register_tool_with_metadata(
    name="redshift",
    file_access=ToolFileAccess.UNCONFINED,
    display_name="Amazon Redshift",
    description="Query Amazon Redshift data warehouse - list tables, run SQL, and export results",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="AwsRedshift",
    icon_color="text-red-600",
    config_fields=[
        ConfigField(
            name="host",
            env=("REDSHIFT_HOST",),
            label="Host",
            type="url",
            required=True,
            placeholder="my-cluster.xxxx.region.redshift.amazonaws.com",
            description="Redshift cluster endpoint",
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
            env=("REDSHIFT_DATABASE",),
            label="Database",
            type="text",
            required=True,
            placeholder="dev",
            description="Database name",
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
            name="iam",
            label="Use IAM Authentication",
            type="boolean",
            required=False,
            default=False,
            description="Use IAM authentication instead of password",
        ),
        ConfigField(
            name="cluster_identifier",
            env=("REDSHIFT_CLUSTER_IDENTIFIER",),
            label="Cluster Identifier",
            type="text",
            required=False,
            default=None,
            description="Redshift cluster identifier (required for IAM auth)",
        ),
        ConfigField(
            name="region",
            env=("AWS_REGION", "AWS_DEFAULT_REGION"),
            label="Region",
            type="text",
            required=False,
            default=None,
            placeholder="us-east-1",
        ),
        ConfigField(
            name="db_user",
            env=("REDSHIFT_DB_USER",),
            label="DB User",
            type="text",
            required=False,
            default=None,
            description="Database user for IAM authentication",
        ),
        ConfigField(
            name="access_key_id",
            env=("AWS_ACCESS_KEY_ID",),
            label="Access Key ID",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="secret_access_key",
            env=("AWS_SECRET_ACCESS_KEY",),
            label="Secret Access Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="session_token",
            env=("AWS_SESSION_TOKEN",),
            label="Session Token",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="profile",
            env=("AWS_PROFILE",),
            label="AWS Profile",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="ssl",
            label="SSL",
            type="boolean",
            required=False,
            default=True,
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
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#redshift",
    function_names=(
        "describe_table",
        "export_table_to_path",
        "inspect_query",
        "run_query",
        "show_tables",
        "summarize_table",
    ),
)
def redshift_tools() -> type[RedshiftTools]:
    """Return Amazon Redshift tools for data warehouse operations."""
    from agno.tools.redshift import RedshiftTools

    return RedshiftTools
