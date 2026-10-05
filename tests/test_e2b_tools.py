"""Tests for E2B local file transfers confined to the agent workspace."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.agent import Agent
from e2b import ConnectionConfig
from e2b.envd.process import process_pb2
from e2b.sandbox_sync.commands.command_handle import CommandHandle
from e2b_code_interpreter.models import Execution, Result

import mindroom.custom_tools.e2b as e2b_module
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.e2b import MindRoomE2BTools
from mindroom.path_confinement import MAX_READ_BYTES
from mindroom.tool_system.metadata import get_tool_by_name
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from e2b.sandbox.commands.command_handle import CommandResult

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


_ENVD_URL = "https://49983-sandbox.e2b.test"
_JUPYTER_URL = "https://49999-sandbox.e2b.test"
_ENVD_TOKEN = "envd-token"  # noqa: S105
_CHUNK = 1 << 20


@dataclass
class _WriteInfo:
    path: str


@dataclass
class _FakeFiles:
    stored: dict[str, bytes] = field(default_factory=dict)
    sparse: dict[str, int] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)
    served: int = 0

    def write(self, path: str, data: bytes) -> _WriteInfo:
        self.stored[path] = data
        return _WriteInfo(path=f"/home/user/{path}")

    def chunks(self, path: str) -> Iterator[bytes]:
        """Serve a stored file whole, or a sparse file lazily in zero-filled chunks."""
        if path in self.stored:
            self.served += len(self.stored[path])
            yield self.stored[path]
            return
        for offset in range(0, self.sparse[path], _CHUNK):
            chunk = bytes(min(_CHUNK, self.sparse[path] - offset))
            self.served += len(chunk)
            yield chunk


@dataclass
class _FakeCommands:
    """Run commands through the SDK's own handle, which keeps every output chunk until the command ends."""

    stdout_bytes: int = 0
    served: int = 0

    def run(
        self,
        _cmd: str,
        *,
        background: bool,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> CommandResult:
        assert not background
        handle = CommandHandle(pid=1, handle_kill=lambda: True, events=self._events())
        return handle.wait(on_stdout=on_stdout, on_stderr=on_stderr)

    def _events(self) -> Iterator[process_pb2.StartResponse]:
        for offset in range(0, self.stdout_bytes, _CHUNK):
            chunk = b"x" * min(_CHUNK, self.stdout_bytes - offset)
            self.served += len(chunk)
            data = process_pb2.ProcessEvent.DataEvent(stdout=chunk)
            yield process_pb2.StartResponse(event=process_pb2.ProcessEvent(data=data))
        end = process_pb2.ProcessEvent.EndEvent(exit_code=0, exited=True)
        yield process_pb2.StartResponse(event=process_pb2.ProcessEvent(end=end))


@dataclass
class _FakeSandbox:
    files: _FakeFiles = field(default_factory=_FakeFiles)
    commands: _FakeCommands = field(default_factory=_FakeCommands)
    envd_api_url: str = _ENVD_URL
    connection_config: ConnectionConfig = field(
        default_factory=lambda: ConnectionConfig(api_key="test", extra_sandbox_headers={"X-Access-Token": _ENVD_TOKEN}),
    )

    @classmethod
    def create(cls, **_kwargs: object) -> _FakeSandbox:
        return cls()

    def get_host(self, port: int) -> str:
        return f"{port}-sandbox.e2b.test"


def _serve_files(files: _FakeFiles) -> Callable[..., object]:
    """Answer the envd file route the way the sandbox does, streaming bodies without buffering them."""

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.copy_with(query=None) == f"{_ENVD_URL}/files"
        assert request.headers["X-Access-Token"] == _ENVD_TOKEN
        assert request.url.params["username"] == "user"
        path = request.url.params["path"]
        files.reads.append(path)
        if path not in files.stored and path not in files.sparse:
            return httpx.Response(404, json={"message": f"path '{path}' does not exist"})
        return httpx.Response(200, content=files.chunks(path))

    return _mock_stream(handle)


def _mock_stream(handle: Callable[[httpx.Request], httpx.Response]) -> Callable[..., object]:
    """Stand in for ``httpx.stream`` with responses from ``handle``."""

    @contextmanager
    def stream(method: str, url: str, *, proxy: object, **kwargs: object) -> Iterator[httpx.Response]:
        assert proxy is None
        with (
            httpx.Client(transport=httpx.MockTransport(handle)) as client,
            client.stream(method, url, **kwargs) as response,
        ):
            yield response

    return stream


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
        tool = MindRoomE2BTools(api_key="test", tool_output_workspace_root=workspace_root)
        monkeypatch.setattr(httpx, "stream", _serve_files(_files(tool)))
        return tool

    return build


def _files(tool: MindRoomE2BTools) -> _FakeFiles:
    assert isinstance(tool.sandbox, _FakeSandbox)
    return tool.sandbox.files


def _error(result: str) -> str:
    payload = json.loads(result)
    assert payload["status"] == "error"
    return payload["message"]


def _plant_escape_links(workspace: Path, outside: Path) -> None:
    (workspace / "leak.env").symlink_to(outside / "secret.env")
    (workspace / "linked").symlink_to(outside)
    (workspace / "dangling.py").symlink_to(outside / "missing.py")
    (workspace / "loop").symlink_to(workspace / "loop")
    (workspace / "root_link").symlink_to(workspace)


def _local_path(requested: str, outside: Path) -> str:
    return str(outside / "secret.env") if requested == "absolute" else requested


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


@pytest.mark.parametrize(
    "requested",
    ["absolute", "../outside/secret.env", "leak.env", "linked/secret.env", "loop", "root_link", "/", "..", ""],
)
def test_upload_rejects_paths_outside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
    requested: str,
) -> None:
    """Uploads never read absolute, parent-escaping, outward-linked, or looping paths."""
    _plant_escape_links(workspace, outside)
    tool = make_tool(workspace)

    assert "Error uploading file" in _error(tool.upload_file(_local_path(requested, outside), "/tmp/x"))  # noqa: S108
    assert _files(tool).stored == {}


@pytest.mark.parametrize("name", ["folder", "pipe"])
def test_upload_rejects_non_regular_files(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    name: str,
) -> None:
    """Uploads only read regular files and never block on a FIFO."""
    (workspace / "folder").mkdir()
    os.mkfifo(workspace / "pipe")
    tool = make_tool(workspace)

    assert "regular file" in _error(tool.upload_file(name))
    assert _files(tool).stored == {}


def test_upload_refuses_oversized_file_before_reading(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """A sparse workspace file above the shared read limit is refused instead of buffered whole."""
    with (workspace / "huge.bin").open("wb") as file:
        file.truncate(MAX_READ_BYTES + 1)
    tool = make_tool(workspace)

    assert "size limit" in _error(tool.upload_file("huge.bin"))
    assert _files(tool).stored == {}


def test_upload_rejects_file_swapped_to_link_after_resolution(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file swapped for an outward link after resolution cannot redirect an upload."""
    (workspace / "report.csv").write_bytes(b"a,b\n")
    tool = make_tool(workspace)
    resolve = e2b_module.resolve_agent_file

    def resolve_then_swap(*args: object, **kwargs: object) -> object:
        authorized = resolve(*args, **kwargs)
        (workspace / "report.csv").unlink()
        (workspace / "report.csv").symlink_to(outside / "secret.env")
        return authorized

    monkeypatch.setattr(e2b_module, "resolve_agent_file", resolve_then_swap)

    assert "Error uploading file" in _error(tool.upload_file("report.csv"))
    assert _files(tool).stored == {}


def test_transfers_require_workspace(make_tool: Callable[[Path | None], MindRoomE2BTools]) -> None:
    """Agents without a workspace cannot transfer local files."""
    tool = make_tool(None)
    _files(tool).stored["/tmp/x"] = b"payload"  # noqa: S108

    assert "requires an agent workspace" in _error(tool.upload_file("report.csv"))
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


def test_download_streams_file_in_chunks(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """A sandbox file below the limit arrives whole even when it spans many chunks."""
    tool = make_tool(workspace)
    _files(tool).sparse["/tmp/data.bin"] = 3 * _CHUNK + 5  # noqa: S108

    assert tool.download_file_from_sandbox("/tmp/data.bin") == "data.bin"  # noqa: S108
    assert (workspace / "data.bin").stat().st_size == 3 * _CHUNK + 5


def test_download_refuses_oversized_file_while_streaming(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """A huge sandbox file is refused once it passes the limit, without buffering it or publishing a partial file."""
    (workspace / "result.bin").write_bytes(b"previous")
    tool = make_tool(workspace)
    _files(tool).sparse["/tmp/huge.bin"] = 8 << 30  # noqa: S108

    message = _error(tool.download_file_from_sandbox("/tmp/huge.bin", "result.bin"))  # noqa: S108

    assert "64 MiB transfer limit" in message
    assert _files(tool).served <= MAX_READ_BYTES + _CHUNK
    assert (workspace / "result.bin").read_bytes() == b"previous"
    assert [entry.name for entry in workspace.iterdir()] == ["result.bin"]


def test_download_reports_missing_sandbox_file(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """A missing sandbox file is a tool error carrying the sandbox's message, and nothing is written."""
    tool = make_tool(workspace)

    assert "path '/tmp/missing.csv' does not exist" in _error(tool.download_file_from_sandbox("/tmp/missing.csv"))  # noqa: S108
    assert list(workspace.iterdir()) == []


def test_read_file_content_returns_text(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """File reads return the decoded sandbox file."""
    tool = make_tool(workspace)
    _files(tool).stored["/tmp/notes.txt"] = "caf\u00e9\n".encode()  # noqa: S108

    assert tool.read_file_content("/tmp/notes.txt") == "caf\u00e9\n"  # noqa: S108
    assert tool.read_file_content("/tmp/notes.txt", encoding="latin-1") == "caf\u00c3\u00a9\n"  # noqa: S108


def test_read_file_content_refuses_oversized_file_while_streaming(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """A huge sandbox file read is refused once it passes the limit instead of being buffered whole."""
    tool = make_tool(workspace)
    _files(tool).sparse["/tmp/huge.log"] = 8 << 30  # noqa: S108

    assert "64 MiB transfer limit" in _error(tool.read_file_content("/tmp/huge.log"))  # noqa: S108
    assert _files(tool).served <= MAX_READ_BYTES + _CHUNK


def test_run_command_refuses_output_past_the_limit_while_streaming(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """A command's output is refused once it passes the limit, before the SDK can keep the rest."""
    tool = make_tool(workspace)
    assert isinstance(tool.sandbox, _FakeSandbox)
    commands = tool.sandbox.commands
    commands.stdout_bytes = 5

    assert json.loads(tool.run_command("printf xxxxx")) == ["STDOUT:\nxxxxx"]

    commands.stdout_bytes = MAX_READ_BYTES + 4 * _CHUNK
    commands.served = 0
    assert "64 MiB transfer limit" in _error(tool.run_command("yes"))
    assert commands.served <= MAX_READ_BYTES + _CHUNK


def test_run_python_code_refuses_output_past_the_limit_while_streaming(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cell's output is refused once it passes the limit, even as the one line the SDK would read whole."""
    tool = make_tool(workspace)
    served = 0

    def cell_output(code: str) -> Iterator[bytes]:
        nonlocal served
        yield b'{"type": "stdout", "text": "hi\\n", "timestamp": "2026-10-05T00:00:00Z"}\n'
        if code == "huge()":
            yield b'{"type": "stdout", "timestamp": "2026-10-05T00:00:00Z", "text": "'
            for _ in range((8 << 30) // _CHUNK):
                served += _CHUNK
                yield b"x" * _CHUNK
            yield b'"}\n'
        yield b'{"type": "result", "text": "2", "is_main_result": true}\n'

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == f"{_JUPYTER_URL}/execute"
        assert request.headers["X-Access-Token"] == _ENVD_TOKEN
        return httpx.Response(200, content=cell_output(json.loads(request.content)["code"]))

    monkeypatch.setattr(httpx, "stream", _mock_stream(handle))

    assert json.loads(tool.run_python_code("small()")) == [
        "Logs:\nLogs(stdout: ['hi\\n'], stderr: [])",
        "Result 1: 2",
    ]
    previous = tool.last_execution

    assert "64 MiB transfer limit" in _error(tool.run_python_code("huge()"))
    assert served <= MAX_READ_BYTES + _CHUNK
    assert tool.last_execution is previous


@pytest.mark.parametrize(
    "requested",
    [
        "absolute",
        "../outside/secret.env",
        "leak.env",
        "linked/plugin.py",
        "dangling.py",
        "loop",
        "root_link",
        "/",
        "..",
    ],
)
def test_download_rejects_paths_outside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
    requested: str,
) -> None:
    """Downloads never write absolute, parent-escaping, outward-linked, or looping paths."""
    _plant_escape_links(workspace, outside)
    tool = make_tool(workspace)
    _files(tool).stored["/tmp/evil.py"] = b"import os"  # noqa: S108
    path = _local_path(requested, outside)

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


@pytest.mark.parametrize("local_path", ["result.csv", "exports/final.csv"])
def test_download_refuses_workspace_replaced_by_link(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
    tmp_path: Path,
    local_path: str,
) -> None:
    """A workspace that worker code replaced with a link after the toolkit was built never redirects a download."""
    tool = make_tool(workspace)
    _files(tool).stored["/tmp/out/result.csv"] = b"x,y\n"  # noqa: S108
    workspace.rename(tmp_path / "moved-workspace")
    workspace.symlink_to(outside, target_is_directory=True)

    assert "Error downloading file" in _error(tool.download_file_from_sandbox("/tmp/out/result.csv", local_path))  # noqa: S108
    assert sorted(entry.name for entry in outside.iterdir()) == ["secret.env"]


def test_png_output_path_saves_inside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
) -> None:
    """PNG results save inside the workspace and keep their image when saving elsewhere is refused."""
    tool = make_tool(workspace)
    tool.last_execution = Execution(results=[Result(png=base64.b64encode(_PNG_BYTES).decode())])
    agent = Agent()

    _plant_escape_links(workspace, outside)

    saved = tool.download_png_result(agent, 0, "charts/plot.png")
    rejected = tool.download_png_result(agent, 0, "linked/plot.png")

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


@pytest.mark.parametrize("requested", ["absolute", "linked/config.yaml", "dangling.py"])
def test_chart_data_rejects_paths_outside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
    requested: str,
) -> None:
    """Chart data never writes outside the workspace."""
    _plant_escape_links(workspace, outside)
    tool = make_tool(workspace)
    tool.last_execution = Execution(results=[Result(chart=_CHART)])

    result = tool.download_chart_data(Agent(), 0, _local_path(requested, outside), add_as_artifact=False)

    assert result.content.startswith("Error extracting chart data")
    assert (outside / "secret.env").read_text(encoding="utf-8") == "TOKEN=secret"
    assert sorted(entry.name for entry in outside.iterdir()) == ["secret.env"]


def test_chart_data_reports_invalid_index(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """An out-of-range negative index is a tool error, not an escaped exception."""
    tool = make_tool(workspace)
    tool.last_execution = Execution(results=[Result(chart=_CHART)])

    assert tool.download_chart_data(Agent(), -5).content.startswith("Error extracting chart data")
    assert list(workspace.iterdir()) == []
