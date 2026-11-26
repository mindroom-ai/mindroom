"""Tests for the native Visualization toolkit and workspace-confined saving."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.credentials.manager import CredentialsManager
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tools.visualization import visualization_tools
from tests.agent_tool_helpers import toolkit_offers
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
        result = json.loads(toolkit.create_chart("bar", data={"a": 1}, filename=filename))
        assert result["status"] == "error", filename
        assert "without directories" in result["error"]
    saved = json.loads(toolkit.create_chart("pie", data={"a": 1, "b": 2}, filename="planted.png"))
    default = json.loads(toolkit.create_chart("line", data={"Jan": 1, "Feb": 3}))
    extensionless = json.loads(toolkit.create_chart("histogram", data=[1, 2, 2, 3], filename="spread"))
    scatter = json.loads(toolkit.create_chart("scatter", x_data=[1, 2], y_data=[3, 4], filename="points.png"))

    assert saved["file_path"] == str(workspace / "charts" / "planted.png")
    assert default["file_path"] == str(workspace / "charts" / "line_chart_2.png")
    assert extensionless["file_path"] == str(workspace / "charts" / "spread.png")
    assert scatter["file_path"] == str(workspace / "charts" / "points.png")
    assert outside.read_bytes() == b"untouched"
    assert not (workspace / "charts" / "planted.png").is_symlink()
    for name in ("planted.png", "line_chart_2.png", "spread.png", "points.png"):
        assert (workspace / "charts" / name).read_bytes().startswith(_PNG_SIGNATURE)
    assert sorted(path.name for path in working_directory.iterdir()) == ["config.yaml"]
    assert (working_directory / "config.yaml").read_text(encoding="utf-8") == "agents: {}\n"


def test_visualization_refuses_an_output_directory_replaced_by_a_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace output directory swapped for a link to another directory is refused instead of followed."""
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "charts").symlink_to(outside)
    toolkit = visualization_tools()(tool_output_workspace_root=workspace)

    result = json.loads(toolkit.create_chart("scatter", x_data=[1, 2], y_data=[3, 4], filename="plot.png"))

    assert result["status"] == "error"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("output_dir", ["/etc", "../outside"])
def test_visualization_refuses_output_dir_outside_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_dir: str,
) -> None:
    """An authored output directory must stay inside the workspace."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    with pytest.raises(ValueError, match="inside the agent workspace"):
        visualization_tools()(tool_output_workspace_root=tmp_path, output_dir=output_dir)


def test_registry_injects_the_workspace_and_exposes_create_chart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registered toolkit receives the agent workspace and offers the consolidated create_chart tool."""
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    toolkit = get_tool_by_name(
        "visualization",
        test_runtime_paths(tmp_path / "runtime"),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        runs_in_worker=False,
        tool_output_workspace_root=workspace,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    function = toolkit_offers(toolkit, "create_chart")

    result = json.loads(function.entrypoint(chart_type="bar", data={"a": 1}, filename="bar.png"))

    assert {"chart_type", "data", "x_data", "y_data", "title", "x_label", "y_label", "bins", "filename"} <= set(
        function.parameters["properties"],
    )
    assert result["file_path"] == str(workspace / "charts" / "bar.png")
    assert (workspace / "charts" / "bar.png").read_bytes().startswith(_PNG_SIGNATURE)


def test_visualization_without_a_workspace_reports_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an agent workspace, charts are refused rather than saved in the working directory."""
    monkeypatch.chdir(tmp_path)
    toolkit = visualization_tools()()

    result = json.loads(toolkit.create_chart("bar", data={"a": 1}))

    assert result == {
        "chart_type": "bar_chart",
        "error": "Saving charts requires an agent workspace",
        "status": "error",
    }
    assert list(tmp_path.iterdir()) == []


def test_visualization_honors_enable_flags(tmp_path: Path) -> None:
    """Disabling a chart type via config flag prevents creating that chart type."""
    toolkit = visualization_tools()(tool_output_workspace_root=tmp_path, enable_create_pie_chart=False)

    result = json.loads(toolkit.create_chart("pie", data={"a": 1}))

    assert result["status"] == "error"
    assert "disabled" in result["error"]


@pytest.mark.asyncio
async def test_visualization_scatter_dict_series_passes_validated_tool_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling create_chart('scatter', data={'x': [1, 2], 'y': [3, 4]}) passes schema and runtime validation."""
    from mindroom.tool_system.agno_toolkits import adapt_toolkit  # noqa: PLC0415
    from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    toolkit = visualization_tools()(tool_output_workspace_root=workspace)
    adapted = adapt_toolkit(toolkit)
    function = next(fn for fn in adapted.functions if fn.name == "create_chart")

    outcome = await execute_tool_call(
        function,
        {"chart_type": "scatter", "data": {"x": [1, 2], "y": [3, 4]}, "filename": "scatter_valid.png"},
        ToolCallContext(run_id="run-1", session_id="sess-1", session_state={}, user_id=None),
        middleware=(),
    )

    assert not outcome.is_error
    payload = json.loads(outcome.content)
    assert payload["status"] == "success"
    assert payload["chart_type"] == "scatter_plot"
    assert payload["data_points"] == 2
    assert (workspace / "charts" / "scatter_valid.png").read_bytes().startswith(_PNG_SIGNATURE)
