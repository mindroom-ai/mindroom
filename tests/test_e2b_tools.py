"""Tests for E2B local file transfers confined to the agent workspace."""

from __future__ import annotations

import base64
import json
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, BinaryIO

import pytest
from agno.agent import Agent
from e2b_code_interpreter.models import Execution, Result

import mindroom.custom_tools.e2b as e2b_module
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.e2b import MindRoomE2BTools
from mindroom.tool_system.metadata import get_tool_by_name
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake"
_CHART = {
    "type": "line",
    "title": "Growth",
    "elements": [{"label": "users", "points": [[1, 10], [2, 20]]}],
    "x_label": "Day",
    "y_label": "Users",
    "x_unit": None,
    "y_unit": None,
    "x_ticks": [1, 2],
    "x_tick_labels": ["1", "2"],
    "y_ticks": [10, 20],
    "y_tick_labels": ["10", "20"],
}


@dataclass
class _WriteInfo:
    path: str


@dataclass
class _FakeFiles:
    stored: dict[str, bytes] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)

    def write(self, path: str, data: BinaryIO) -> _WriteInfo:
        self.stored[path] = data.read()
        return _WriteInfo(path=f"/home/user/{path}")

    def read(self, path: str, format: str = "text") -> bytearray:  # noqa: A002
        assert format == "bytes"
        self.reads.append(path)
        return bytearray(self.stored[path])


@dataclass
class _FakeSandbox:
    files: _FakeFiles = field(default_factory=_FakeFiles)

    @classmethod
    def create(cls, **_kwargs: object) -> _FakeSandbox:
        return cls()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Agent workspace root."""
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """Directory beside the workspace holding a secret."""
    path = tmp_path / "outside"
    path.mkdir()
    (path / "secret.env").write_text("TOKEN=secret", encoding="utf-8")
    return path


@pytest.fixture
def make_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[Path | None], MindRoomE2BTools]:
    """Build toolkits backed by an in-memory sandbox."""
    monkeypatch.setattr("agno.tools.e2b.Sandbox", _FakeSandbox)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    def build(workspace_root: Path | None) -> MindRoomE2BTools:
        return MindRoomE2BTools(api_key="test", tool_output_workspace_root=workspace_root)

    return build


def _files(tool: MindRoomE2BTools) -> _FakeFiles:
    assert isinstance(tool.sandbox, _FakeSandbox)
    return tool.sandbox.files


def _error(result: str) -> str:
    payload = json.loads(result)
    assert payload["status"] == "error"
    return payload["message"]


def test_registry_injects_workspace_into_model_entrypoints(
    make_tool: Callable[[Path | None], MindRoomE2BTools],  # noqa: ARG001  # installs the fake sandbox
    tmp_path: Path,
    workspace: Path,
    outside: Path,
) -> None:
    """The registered e2b toolkit receives the agent workspace and confines its model entrypoints."""
    (workspace / "report.csv").write_text("a,b\n", encoding="utf-8")
    tool = get_tool_by_name(
        "e2b",
        test_runtime_paths(tmp_path / "runtime"),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        credential_overrides={"api_key": "test"},
        disable_sandbox_proxy=True,
        tool_output_workspace_root=workspace,
        worker_target=None,
    )
    upload = tool.functions["upload_file"].entrypoint
    assert isinstance(tool, MindRoomE2BTools)
    assert upload is not None

    assert upload("report.csv") == "/home/user/report.csv"
    assert "Error uploading file" in _error(upload(str(outside / "secret.env")))
    assert _files(tool).stored == {"report.csv": b"a,b\n"}


def test_upload_reads_workspace_relative_file(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """Uploads read workspace-relative files."""
    (workspace / "data").mkdir()
    (workspace / "data" / "report.csv").write_bytes(b"a,b\n")
    tool = make_tool(workspace)

    assert tool.upload_file("data/report.csv", "workspace/report.csv") == "/home/user/workspace/report.csv"
    assert _files(tool).stored == {"workspace/report.csv": b"a,b\n"}


def test_upload_follows_links_that_stay_inside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """Uploads follow symlinks whose targets stay inside the workspace."""
    (workspace / "docs").mkdir()
    (workspace / "docs" / "notes.md").write_bytes(b"notes")
    (workspace / "knowledge").symlink_to(workspace / "docs")
    tool = make_tool(workspace)

    assert tool.upload_file("knowledge/notes.md") == "/home/user/notes.md"
    assert _files(tool).stored == {"notes.md": b"notes"}


@pytest.mark.parametrize("requested", ["absolute", "../outside/secret.env", "leak.env", "linked/secret.env"])
def test_upload_rejects_paths_outside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
    requested: str,
) -> None:
    """Uploads never read absolute, parent-escaping, or outward-linked paths."""
    (workspace / "leak.env").symlink_to(outside / "secret.env")
    (workspace / "linked").symlink_to(outside)
    tool = make_tool(workspace)
    path = str(outside / "secret.env") if requested == "absolute" else requested

    assert "Error uploading file" in _error(tool.upload_file(path, "/tmp/x"))  # noqa: S108
    assert _files(tool).stored == {}


def test_upload_rejects_non_regular_files(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """Uploads only read regular files."""
    (workspace / "folder").mkdir()
    tool = make_tool(workspace)

    assert "regular file" in _error(tool.upload_file("folder"))
    assert _files(tool).stored == {}


def test_transfers_require_workspace(make_tool: Callable[[Path | None], MindRoomE2BTools]) -> None:
    """Agents without a workspace cannot transfer local files."""
    tool = make_tool(None)
    _files(tool).stored["/tmp/x"] = b"payload"  # noqa: S108

    assert "require an agent workspace" in _error(tool.upload_file("report.csv"))
    assert "require an agent workspace" in _error(tool.download_file_from_sandbox("/tmp/x"))  # noqa: S108
    assert _files(tool).reads == []


def test_download_writes_inside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Downloads land in the workspace, never the process working directory."""
    monkeypatch.chdir(tmp_path)
    tool = make_tool(workspace)
    _files(tool).stored["/tmp/out/result.csv"] = b"x,y\n"  # noqa: S108

    assert tool.download_file_from_sandbox("/tmp/out/result.csv") == "result.csv"  # noqa: S108
    assert tool.download_file_from_sandbox("/tmp/out/result.csv", "exports/final.csv") == "exports/final.csv"  # noqa: S108
    assert (workspace / "result.csv").read_bytes() == b"x,y\n"
    assert (workspace / "exports" / "final.csv").read_bytes() == b"x,y\n"
    assert not (tmp_path / "result.csv").exists()


@pytest.mark.parametrize(
    "requested",
    ["absolute", "../outside/secret.env", "leak.env", "linked/plugin.py", "/", ".."],
)
def test_download_rejects_paths_outside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
    requested: str,
) -> None:
    """Downloads never write absolute, parent-escaping, or outward-linked paths."""
    (workspace / "leak.env").symlink_to(outside / "secret.env")
    (workspace / "linked").symlink_to(outside)
    tool = make_tool(workspace)
    _files(tool).stored["/tmp/evil.py"] = b"import os"  # noqa: S108
    path = str(outside / "secret.env") if requested == "absolute" else requested

    assert "Error downloading file" in _error(tool.download_file_from_sandbox("/tmp/evil.py", path))  # noqa: S108
    assert _files(tool).reads == []
    assert (outside / "secret.env").read_text(encoding="utf-8") == "TOKEN=secret"
    assert sorted(entry.name for entry in outside.iterdir()) == ["secret.env"]


def test_download_rejects_parent_swapped_to_link_after_resolution(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent swapped for an outward link after resolution cannot redirect a download."""
    (workspace / "plugins").mkdir()
    tool = make_tool(workspace)
    _files(tool).stored["/tmp/evil.py"] = b"import os"  # noqa: S108
    resolve = e2b_module.resolve_path_within_root

    def resolve_then_swap(root: Path, path: Path, **kwargs: object) -> Path:
        resolved = resolve(root, path, **kwargs)
        (workspace / "plugins").rmdir()
        (workspace / "plugins").symlink_to(outside)
        return resolved

    monkeypatch.setattr(e2b_module, "resolve_path_within_root", resolve_then_swap)

    assert "Error downloading file" in _error(tool.download_file_from_sandbox("/tmp/evil.py", "plugins/x.py"))  # noqa: S108
    assert not (outside / "x.py").exists()


def test_png_output_path_saves_inside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
) -> None:
    """PNG results save inside the workspace and keep their image when saving elsewhere is refused."""
    tool = make_tool(workspace)
    tool.last_execution = Execution(results=[Result(png=base64.b64encode(_PNG_BYTES).decode())])
    agent = Agent()

    saved = tool.download_png_result(agent, 0, "charts/plot.png")
    rejected = tool.download_png_result(agent, 0, str(outside / "plot.png"))

    assert saved.content.endswith("and saved to charts/plot.png")
    assert saved.images
    assert (workspace / "charts" / "plot.png").read_bytes() == _PNG_BYTES
    assert "saving it failed" in rejected.content
    assert rejected.images
    assert not (outside / "plot.png").exists()


def test_chart_data_saves_inside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Chart data defaults to the workspace, never the process working directory."""
    monkeypatch.chdir(tmp_path)
    tool = make_tool(workspace)
    tool.last_execution = Execution(results=[Result(chart=_CHART, png=base64.b64encode(_PNG_BYTES).decode())])

    result = tool.download_chart_data(Agent())

    assert result.content.splitlines()[:4] == [
        "Interactive line chart data saved to chart-data-0.json",
        "Title: Growth",
        "X-axis: Day",
        "Y-axis: Users",
    ]
    assert result.images
    assert json.loads((workspace / "chart-data-0.json").read_text(encoding="utf-8"))["title"] == "Growth"
    assert not (tmp_path / "chart-data-0.json").exists()


def test_chart_data_rejects_paths_outside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
) -> None:
    """Chart data never writes outside the workspace."""
    tool = make_tool(workspace)
    tool.last_execution = Execution(results=[Result(chart=_CHART)])

    result = tool.download_chart_data(Agent(), 0, str(outside / "config.yaml"), add_as_artifact=False)

    assert result.content.startswith("Error extracting chart data")
    assert not (outside / "config.yaml").exists()
