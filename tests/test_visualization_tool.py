"""Tests for where the Visualization toolkit saves charts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.credentials import CredentialsManager
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tools.visualization import visualization_tools
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def test_visualization_saves_only_inside_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Model-chosen names land in the workspace output directory, never in the working directory or through links."""
    working_directory = tmp_path / "app"
    working_directory.mkdir()
    (working_directory / "config.yaml").write_text("agents: {}\n", encoding="utf-8")
    monkeypatch.chdir(working_directory)
    workspace = tmp_path / "workspace"
    (workspace / "charts").mkdir(parents=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"untouched")
    (workspace / "charts" / "planted.png").symlink_to(outside)
    toolkit = visualization_tools()(tool_output_workspace_root=workspace)

    for filename in ("../../app/config.yaml", str(working_directory / "config.yaml"), "..", ""):
        result = json.loads(toolkit.create_bar_chart({"a": 1}, filename=filename))
        assert result["status"] == "error", filename
        assert "without directories" in result["error"]
    saved = json.loads(toolkit.create_pie_chart({"a": 1, "b": 2}, filename="planted.png"))
    default = json.loads(toolkit.create_line_chart({"Jan": 1, "Feb": 3}))
    extensionless = json.loads(toolkit.create_histogram([1, 2, 2, 3], filename="spread"))

    assert saved["file_path"] == str(workspace / "charts" / "planted.png")
    assert default["file_path"] == str(workspace / "charts" / "line_chart_2.png")
    assert extensionless["file_path"] == str(workspace / "charts" / "spread.png")
    assert outside.read_bytes() == b"untouched"
    assert not (workspace / "charts" / "planted.png").is_symlink()
    for name in ("planted.png", "line_chart_2.png", "spread.png"):
        assert (workspace / "charts" / name).read_bytes().startswith(_PNG_SIGNATURE)
    assert sorted(path.name for path in working_directory.iterdir()) == ["config.yaml"]
    assert (working_directory / "config.yaml").read_text(encoding="utf-8") == "agents: {}\n"


def test_visualization_refuses_an_output_directory_replaced_by_a_link(tmp_path: Path) -> None:
    """A workspace output directory swapped for a link to another directory is refused instead of followed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "charts").symlink_to(outside)
    toolkit = visualization_tools()(tool_output_workspace_root=workspace)

    result = json.loads(toolkit.create_scatter_plot(x=[1, 2], y=[3, 4], filename="plot.png"))

    assert result["status"] == "error"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("output_dir", ["/etc", "../outside"])
def test_visualization_refuses_output_dir_outside_the_workspace(tmp_path: Path, output_dir: str) -> None:
    """An authored output directory must stay inside the workspace."""
    with pytest.raises(ValueError, match="inside the agent workspace"):
        visualization_tools()(tool_output_workspace_root=tmp_path, output_dir=output_dir)


def test_registry_injects_the_workspace_and_keeps_chart_schemas(tmp_path: Path) -> None:
    """The registered toolkit receives the agent workspace and still advertises the upstream chart parameters."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    toolkit = get_tool_by_name(
        "visualization",
        test_runtime_paths(tmp_path / "runtime"),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        disable_sandbox_proxy=True,
        tool_output_workspace_root=workspace,
        worker_target=None,
    )
    function = toolkit.functions["create_bar_chart"]
    function.process_entrypoint()
    schema = function.to_dict()
    assert function.entrypoint is not None

    result = json.loads(function.entrypoint(data={"a": 1}, filename="bar.png"))

    assert schema["description"] == "Create a bar chart from the provided data."
    assert {"data", "title", "x_label", "y_label", "filename"} <= set(schema["parameters"]["properties"])
    assert result["file_path"] == str(workspace / "charts" / "bar.png")
    assert (workspace / "charts" / "bar.png").read_bytes().startswith(_PNG_SIGNATURE)


def test_visualization_without_a_workspace_reports_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an agent workspace, charts are refused rather than saved in the working directory."""
    monkeypatch.chdir(tmp_path)
    toolkit = visualization_tools()()

    result = json.loads(toolkit.create_bar_chart({"a": 1}))

    assert result == {
        "chart_type": "bar_chart",
        "error": "Saving charts requires an agent workspace",
        "status": "error",
    }
    assert list(tmp_path.iterdir()) == []
