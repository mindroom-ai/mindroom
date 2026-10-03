"""Airflow DAG-file toolkit whose reads and writes follow the agent's ``file_access``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from agno.tools.airflow import AirflowTools
from agno.utils.log import log_error

from mindroom.file_access import resolve_agent_file
from mindroom.path_confinement import read_regular_file_within_root
from mindroom.tools.path_safety import write_agent_file

if TYPE_CHECKING:
    from mindroom.config.models import FileAccess


# AGNO_COMPAT: AirflowTools resolves DAG files from the process working directory and reaches them by path.
# Reason: Agno 3.0.9 defaults dags_dir to the working directory, which can hold MindRoom's config.yaml
# and .env, and reads and writes by path after a resolve-time check, so a link planted in a shared
# workspace could redirect the call.
# Upstream issue: Tracking gap; upstream tracking has not been verified.
# Upstream PR: None identified.
# Remove when: AirflowTools accepts a caller-supplied base directory descriptor or file reader and writer;
# retain DAG directories below the agent workspace and resolution under file_access.
# Coverage: tests/test_airflow_tools.py and
# tests/test_file_access_contract.py::test_file_swapped_for_link_after_the_check_is_refused.
class MindRoomAirflowTools(AirflowTools):
    """Resolve DAG files from a ``dags_dir`` relative to the agent workspace, where the agent's ``file_access`` allows.

    Reads open the file through no-follow descriptors and refuse it above the shared read cap; writes replace it atomically without following links below the workspace.
    """

    def __init__(
        self,
        dags_dir: Path | str | None = None,
        enable_save_dag_file: bool = True,
        enable_read_dag_file: bool = True,
        all: bool = False,  # noqa: A002 - upstream option name
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        super().__init__(
            dags_dir=(tool_output_workspace_root or Path.cwd()) / (dags_dir or ""),
            enable_save_dag_file=enable_save_dag_file,
            enable_read_dag_file=enable_read_dag_file,
            all=all,
            **kwargs,
        )

    @override
    def save_dag_file(self, contents: str, dag_file: str) -> str:
        """Saves python code for an Airflow DAG to a file called `dag_file` and returns the file path if successful.

        :param contents: The contents of the DAG.
        :param dag_file: The file to save to, relative to the DAG directory; with ``file_access: workspace`` it must stay inside the agent workspace.
        :return: The file path if successful, otherwise returns an error message.
        """
        try:
            file_path = write_agent_file(
                str(self.dags_dir / dag_file),
                contents.encode(),
                workspace_root=self._workspace_root,
                file_access=self._file_access,
            )
        except Exception as e:
            log_error(f"Error saving to file: {e}")
            return f"Error saving to file: {e}"
        return str(file_path)

    @override
    def read_dag_file(self, dag_file: str) -> str:
        """Reads an Airflow DAG file `dag_file` and returns the contents if successful.

        :param dag_file: The file to read, relative to the DAG directory; with ``file_access: workspace`` it must be inside the agent workspace.
        :return: The contents of the file if successful, otherwise returns an error message.
        """
        try:
            authorized = resolve_agent_file(
                str(self.dags_dir / dag_file),
                workspace_root=self._workspace_root,
                file_access=self._file_access,
                field_name="dag_file",
            )
            return read_regular_file_within_root(authorized.root, authorized.relative).decode()
        except Exception as e:
            log_error(f"Error reading file: {e}")
            return f"Error reading file: {e}"
