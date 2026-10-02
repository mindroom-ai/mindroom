"""Airflow DAG files stay where the agent's file_access allows."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from mindroom.tools.airflow import airflow_tools

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_dag_files_follow_file_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DAG paths resolve from the workspace and never reach the primary's working directory."""
    workspace = tmp_path / "workspace"
    (workspace / "dags").mkdir(parents=True)
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (primary / "config.yaml").write_text("administrators: []\n", encoding="utf-8")
    (workspace / "dags" / "linked.py").symlink_to(primary / "config.yaml")
    monkeypatch.chdir(primary)
    toolkit_class = airflow_tools()
    tool = toolkit_class(dags_dir="dags", tool_output_workspace_root=workspace)

    saved = tool.save_dag_file("dag = 1\n", "generated/new_job.py")

    assert saved == str((workspace / "dags" / "generated" / "new_job.py").resolve())
    assert tool.read_dag_file("generated/new_job.py") == "dag = 1\n"
    escapes = (str(primary / "config.yaml"), os.path.relpath(primary / "config.yaml", workspace / "dags"), "linked.py")
    for dag_file in escapes:
        assert tool.read_dag_file(dag_file).startswith("Error reading file:"), dag_file
        assert tool.save_dag_file("administrators: [attacker]\n", dag_file).startswith("Error saving to file:"), (
            dag_file
        )
    without_workspace = toolkit_class()
    assert without_workspace.read_dag_file(".env").startswith("Error reading file:")
    assert without_workspace.save_dag_file("administrators: [attacker]\n", "config.yaml").startswith("Error saving")

    assert (primary / "config.yaml").read_text(encoding="utf-8") == "administrators: []\n"
    assert sorted(entry.name for entry in primary.iterdir()) == [".env", "config.yaml"]
