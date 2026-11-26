"""Airflow DAG-file toolkit whose reads and writes follow the agent's ``file_access``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.file_access import resolve_agent_file, write_agent_file
from mindroom.logging_config import get_logger
from mindroom.path_confinement import read_regular_file_within_root
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import UNSET, UnsetType, resolve_toggle

if TYPE_CHECKING:
    from mindroom.config.models import FileAccess

logger = get_logger(__name__)


class AirflowTools(Toolkit):
    """Read and write Airflow DAG files relative to the agent workspace under ``file_access``."""

    def __init__(
        self,
        dags_dir: Path | str | None = None,
        enable_save_dag_file: bool | None | UnsetType = UNSET,
        enable_read_dag_file: bool | None | UnsetType = UNSET,
        all: bool | None | UnsetType = UNSET,  # noqa: A002
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._file_access: FileAccess = file_access
        self.dags_dir = (tool_output_workspace_root or Path.cwd()) / (dags_dir or "")

        enable_all = resolve_toggle(all, default=False)
        tools: list[Any] = []
        if resolve_toggle(enable_save_dag_file, default=True, enable_all=enable_all):
            tools.append(self.save_dag_file)
        if resolve_toggle(enable_read_dag_file, default=True, enable_all=enable_all):
            tools.append(self.read_dag_file)
        super().__init__(name="airflow", tools=tools)

    def save_dag_file(self, contents: str, dag_file: str) -> str:
        """Write Python DAG `contents` to `dag_file` under the DAG directory and return its path."""
        try:
            file_path = write_agent_file(
                str(self.dags_dir / dag_file),
                contents.encode(),
                workspace_root=self._workspace_root,
                file_access=self._file_access,
            )
        except Exception as exc:
            logger.warning("airflow_save_dag_failed", error=str(exc))
            return f"Error saving to file: {exc}"
        return str(file_path)

    def read_dag_file(self, dag_file: str) -> str:
        """Read and return the contents of `dag_file` under the DAG directory."""
        try:
            authorized = resolve_agent_file(
                str(self.dags_dir / dag_file),
                workspace_root=self._workspace_root,
                file_access=self._file_access,
                field_name="dag_file",
            )
            return read_regular_file_within_root(authorized.root, authorized.relative).decode()
        except Exception as exc:
            logger.warning("airflow_read_dag_failed", error=str(exc))
            return f"Error reading file: {exc}"


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
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/automation-and-platforms/#airflow",
    function_names=("read_dag_file", "save_dag_file"),
)
def airflow_tools() -> type[AirflowTools]:
    """Return Airflow tools whose DAG files follow the agent's file_access."""
    return AirflowTools
