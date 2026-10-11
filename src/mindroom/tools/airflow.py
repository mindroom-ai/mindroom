"""Airflow tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.tools.agno_compat_airflow import MindRoomAirflowTools


@register_tool_with_metadata(
    name="airflow",
    file_access=ToolFileAccess.AGENT,
    display_name="Airflow",
    description="Apache Airflow DAG file management for workflow orchestration",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaCog",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="dags_dir",
            label="Dags Dir",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_save_dag_file",
            label="Enable Save Dag File",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_read_dag_file",
            label="Enable Read Dag File",
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
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT, ToolManagedInitArg.FILE_ACCESS),
    dependencies=[],  # No additional dependencies required beyond agno
    docs_url="https://docs.agno.com/tools/toolkits/others/airflow",
    function_names=("read_dag_file", "save_dag_file"),
)
def airflow_tools() -> type[MindRoomAirflowTools]:
    """Return Airflow tools whose DAG files follow the agent's file_access."""
    from mindroom.tools.agno_compat_airflow import MindRoomAirflowTools

    return MindRoomAirflowTools
