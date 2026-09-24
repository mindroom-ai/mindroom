"""Pandas tools configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolExecutionTarget, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.pandas import PandasTools


@register_tool_with_metadata(
    name="pandas",
    display_name="Pandas",
    description="Advanced data manipulation and analysis",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
    consumes_workspace_paths=True,
    icon="Database",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="base_dir",
            label="Base Dir",
            type="text",
            required=False,
            default=None,
            authored_override=False,
        ),
        ConfigField(
            name="enable_create_pandas_dataframe",
            label="Enable Create Pandas Dataframe",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_run_dataframe_operation",
            label="Enable Run Dataframe Operation",
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
    dependencies=["pandas"],
    docs_url="https://docs.agno.com/tools/toolkits/database/pandas",
    function_names=("create_pandas_dataframe", "run_dataframe_operation"),
)
def pandas_tools() -> type[PandasTools]:
    """Return allow-listed Pandas tools confined to the agent workspace."""
    from mindroom.custom_tools.pandas import PandasTools

    return PandasTools
