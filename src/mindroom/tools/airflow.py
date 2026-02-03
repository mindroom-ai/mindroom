"""Airflow tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.airflow import AirflowTools


@register_tool_with_metadata(
    name="airflow",
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
    dependencies=[],  # No additional dependencies required beyond agno
    docs_url="https://docs.agno.com/tools/toolkits/others/airflow",
)
def airflow_tools() -> type[AirflowTools]:
    """Return Airflow tools for DAG file management."""
    from agno.tools.airflow import AirflowTools

    return AirflowTools
