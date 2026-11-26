"""Tests for native E2B toolkit with workspace-confined transfers and streaming output caps."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import e2b_code_interpreter
import httpx
import pytest
from e2b import ConnectionConfig
from e2b.envd.process import process_pb2
from e2b.sandbox_sync.commands.command_handle import CommandHandle
from e2b_code_interpreter.models import Execution, Result

import mindroom.custom_tools.e2b as e2b_module
from mindroom.credentials.manager import CredentialsManager
from mindroom.custom_tools.e2b import MindRoomE2BTools
from mindroom.path_confinement import MAX_READ_BYTES
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_function import ToolValue
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
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
    reads: Sequence[str] = field(default_factory=list)
    served: int = 0

    def write(self, path: str, data: bytes) -> _WriteInfo:
        self.stored[path] = data
        return _WriteInfo(path=f"/home/user/{path}")

    def list(self, _path: str) -> Sequence[object]:
        return [
            type("_Entry", (), {"name": name, "type": "file", "size": len(data)})()
            for name, data in self.stored.items()
        ]

    def chunks(self, path: str) -> Iterator[bytes]:
        if path in self.stored:
            self.served += len(self.stored[path])
            yield self.stored[path]
            return
        for offset in range(0, self.sparse[path], _CHUNK):
            chunk = bytes(min(_CHUNK, self.sparse[path] - offset))
            self.served += len(chunk)
            yield chunk


@dataclass
class _FakeBackgroundHandle:
    pid: int
    killed: bool = False

    def kill(self) -> bool:
        self.killed = True
        return True


@dataclass
class _FakeCommands:
    stdout_bytes: int = 0
    served: int = 0
    bg_handles: list[_FakeBackgroundHandle] = field(default_factory=list)

    def run(
        self,
        _cmd: str,
        *,
        background: bool,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> CommandResult | _FakeBackgroundHandle:
        if background:
            handle = _FakeBackgroundHandle(pid=100 + len(self.bg_handles))
            self.bg_handles.append(handle)
            return handle
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
    sandbox_id: str = "sbx-123"
    files: _FakeFiles = field(default_factory=_FakeFiles)
    commands: _FakeCommands = field(default_factory=_FakeCommands)
    envd_api_url: str = _ENVD_URL
    timeout_set: int | None = None
    killed: bool = False
    connection_config: ConnectionConfig = field(
        default_factory=lambda: ConnectionConfig(api_key="test", extra_sandbox_headers={"X-Access-Token": _ENVD_TOKEN}),
    )

    @classmethod
    def create(cls, **_kwargs: object) -> _FakeSandbox:
        return cls()

    def get_host(self, port: int) -> str:
        return f"{port}-sandbox.e2b.test"

    def set_timeout(self, timeout: int) -> None:
        self.timeout_set = timeout

    def kill(self) -> bool:
        self.killed = True
        return True


def _serve_files(files: _FakeFiles) -> Callable[..., object]:
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
    monkeypatch.setattr(e2b_code_interpreter, "Sandbox", _FakeSandbox)
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


def _error(result: str | ToolValue) -> str:
    assert isinstance(result, str)
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


def test_e2b_rejects_ambient_only_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """MindRoomE2BTools fails closed without an explicit scoped key even if E2B_API_KEY is in host env."""
    monkeypatch.setenv("E2B_API_KEY", "ambient-e2b-key")
    with pytest.raises(ValueError, match="E2B API key is required"):
        MindRoomE2BTools()


def test_registry_injects_workspace_into_model_entrypoints(
    make_tool: Callable[[Path | None], MindRoomE2BTools],  # noqa: ARG001
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
        runs_in_worker=False,
        tool_output_workspace_root=workspace,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    assert isinstance(tool, MindRoomE2BTools)
    e2b_files = tool.functions["e2b_files"]

    assert e2b_files("upload", file_path="report.csv") == "/home/user/report.csv"
    assert "Error uploading file" in _error(e2b_files("upload", file_path=str(outside / "secret.env")))
    assert _files(tool).stored == {"report.csv": b"a,b\n"}


def test_upload_reads_workspace_relative_file(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """Uploads read workspace-relative files."""
    (workspace / "data").mkdir()
    (workspace / "data" / "report.csv").write_bytes(b"a,b\n")
    tool = make_tool(workspace)

    assert (
        tool.e2b_files("upload", file_path="data/report.csv", sandbox_path="workspace/report.csv")
        == "/home/user/workspace/report.csv"
    )
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

    assert tool.e2b_files("upload", file_path="knowledge/notes.md") == "/home/user/notes.md"
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

    assert "Error uploading file" in _error(
        tool.e2b_files("upload", file_path=_local_path(requested, outside), sandbox_path="/tmp/x"),  # noqa: S108
    )
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

    assert "regular file" in _error(tool.e2b_files("upload", file_path=name))
    assert _files(tool).stored == {}


def test_upload_refuses_oversized_file_before_reading(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """A sparse workspace file above the shared read limit is refused instead of buffered whole."""
    with (workspace / "huge.bin").open("wb") as file:
        file.truncate(MAX_READ_BYTES + 1)
    tool = make_tool(workspace)

    assert "size limit" in _error(tool.e2b_files("upload", file_path="huge.bin"))
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

    assert "Error uploading file" in _error(tool.e2b_files("upload", file_path="report.csv"))
    assert _files(tool).stored == {}


def test_transfers_require_workspace(make_tool: Callable[[Path | None], MindRoomE2BTools]) -> None:
    """Agents without a workspace cannot transfer local files."""
    tool = make_tool(None)
    _files(tool).stored["/tmp/x"] = b"payload"  # noqa: S108

    assert "requires an agent workspace" in _error(tool.e2b_files("upload", file_path="report.csv"))
    assert "require an agent workspace" in _error(tool.e2b_files("download", sandbox_path="/tmp/x"))  # noqa: S108
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

    assert tool.e2b_files("download", sandbox_path="/tmp/out/result.csv") == "result.csv"  # noqa: S108
    assert (
        tool.e2b_files("download", sandbox_path="/tmp/out/result.csv", local_path="exports/final.csv")  # noqa: S108
        == "exports/final.csv"
    )
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

    assert tool.e2b_files("download", sandbox_path="/tmp/data.bin") == "data.bin"  # noqa: S108
    assert (workspace / "data.bin").stat().st_size == 3 * _CHUNK + 5


def test_download_refuses_oversized_file_while_streaming(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """A huge sandbox file is refused once it passes the limit, without buffering it or publishing a partial file."""
    (workspace / "result.bin").write_bytes(b"previous")
    tool = make_tool(workspace)
    _files(tool).sparse["/tmp/huge.bin"] = 8 << 30  # noqa: S108

    message = _error(tool.e2b_files("download", sandbox_path="/tmp/huge.bin", local_path="result.bin"))  # noqa: S108

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

    assert "path '/tmp/missing.csv' does not exist" in _error(
        tool.e2b_files("download", sandbox_path="/tmp/missing.csv"),  # noqa: S108
    )
    assert list(workspace.iterdir()) == []


def test_read_file_returns_text_and_refuses_oversized(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """File reads decode text and refuse huge sandbox files while streaming."""
    tool = make_tool(workspace)
    _files(tool).stored["/tmp/notes.txt"] = "caf\u00e9\n".encode()  # noqa: S108
    _files(tool).sparse["/tmp/huge.log"] = 8 << 30  # noqa: S108

    assert tool.e2b_inspect("read_file", path="/tmp/notes.txt") == "caf\u00e9\n"  # noqa: S108
    assert tool.e2b_inspect("read_file", path="/tmp/notes.txt", encoding="latin-1") == "caf\u00c3\u00a9\n"  # noqa: S108
    _files(tool).served = 0
    assert "64 MiB transfer limit" in _error(tool.e2b_inspect("read_file", path="/tmp/huge.log"))  # noqa: S108
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

    assert json.loads(tool.e2b_run("command", command="printf xxxxx")) == ["STDOUT:\nxxxxx"]

    commands.stdout_bytes = MAX_READ_BYTES + 4 * _CHUNK
    commands.served = 0
    assert "64 MiB transfer limit" in _error(tool.e2b_run("command", command="yes"))
    assert commands.served <= MAX_READ_BYTES + _CHUNK


def test_run_python_code_refuses_output_past_the_limit_while_streaming(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cell's output is refused once it passes the limit, preserving last_execution."""
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

    assert json.loads(tool.e2b_run("python", code="small()")) == [
        "Logs:\nLogs(stdout: ['hi\\n'], stderr: [])",
        "Result 1: 2",
    ]
    previous = tool.last_execution

    assert "64 MiB transfer limit" in _error(tool.e2b_run("python", code="huge()"))
    assert served <= MAX_READ_BYTES + _CHUNK
    assert tool.last_execution is previous


@pytest.mark.parametrize(
    ("timeout", "message"),
    [
        (httpx.ReadTimeout("timed out"), "Execution timed out"),
        (httpx.ConnectTimeout("timed out"), "Request timed out"),
    ],
)
def test_run_python_code_reports_timeouts_like_the_sdk(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout: httpx.TimeoutException,
    message: str,
) -> None:
    """A cell that stays silent too long, or a sandbox that cannot be reached, reports the SDK's timeout message."""
    tool = make_tool(workspace)

    def handle(_request: httpx.Request) -> httpx.Response:
        raise timeout

    monkeypatch.setattr(httpx, "stream", _mock_stream(handle))

    assert message in _error(tool.e2b_run("python", code="train()"))


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

    assert "Error downloading file" in _error(
        tool.e2b_files("download", sandbox_path="/tmp/evil.py", local_path=path),  # noqa: S108
    )
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

    assert "Error downloading file" in _error(
        tool.e2b_files("download", sandbox_path="/tmp/evil.py", local_path="plugins/x.py"),  # noqa: S108
    )
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

    assert "Error downloading file" in _error(
        tool.e2b_files("download", sandbox_path="/tmp/out/result.csv", local_path=local_path),  # noqa: S108
    )
    assert sorted(entry.name for entry in outside.iterdir()) == ["secret.env"]


def test_png_and_chart_artifacts_return_native_tool_value_and_stay_in_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PNG and chart results return MindRoom ToolValue media and save only inside the workspace."""
    monkeypatch.chdir(tmp_path)
    tool = make_tool(workspace)
    tool.last_execution = Execution(results=[Result(chart=_CHART, png=base64.b64encode(_PNG_BYTES).decode())])
    _plant_escape_links(workspace, outside)

    saved = tool.e2b_files("download_png", result_index=0, local_path="charts/plot.png")
    rejected = tool.e2b_files("download_png", result_index=0, local_path="linked/plot.png")
    assert isinstance(saved, ToolValue)
    assert isinstance(rejected, ToolValue)
    assert saved.content.endswith("and saved to charts/plot.png")
    assert saved.images[0].content == _PNG_BYTES
    assert (workspace / "charts" / "plot.png").read_bytes() == _PNG_BYTES
    assert "saving it failed" in rejected.content
    assert rejected.images
    assert not (outside / "plot.png").exists()

    chart_res = tool.e2b_files("download_chart", result_index=0)
    assert isinstance(chart_res, ToolValue)
    assert chart_res.content.splitlines()[:4] == [
        "Interactive line chart data saved to chart-data-0.json",
        "Title: Growth",
        "X-axis: Day",
        "Y-axis: Users",
    ]
    assert chart_res.images
    assert json.loads((workspace / "chart-data-0.json").read_text(encoding="utf-8"))["title"] == "Growth"
    assert not (tmp_path / "chart-data-0.json").exists()

    bad_index = tool.e2b_files("download_chart", result_index=-5)
    assert isinstance(bad_index, ToolValue)
    assert bad_index.content.startswith("Error extracting chart data")


def test_background_commands_inspect_and_lifecycle(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    workspace: Path,
) -> None:
    """Background commands track PIDs cleanly, and inspect/lifecycle actions work natively."""
    tool = make_tool(workspace)
    started = json.loads(tool.e2b_run("background_command", command="sleep 10"))
    pid = started["pid"]
    assert "terminated successfully" in tool.e2b_run("kill_background", pid=pid)
    assert "Unknown background command" in _error(tool.e2b_run("kill_background", pid=pid))

    assert tool.e2b_inspect("status") == "sbx-123"
    assert tool.e2b_inspect("public_url", port=8000) == "http://8000-sandbox.e2b.test"
    assert tool.e2b_lifecycle("set_timeout", timeout=120) == "120"
    assert isinstance(tool.sandbox, _FakeSandbox)
    assert tool.sandbox.timeout_set == 120
    assert json.loads(tool.e2b_lifecycle("shutdown"))["status"] == "success"


def test_e2b_pins_domain_and_fails_closed_on_ambient_e2b_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2B pins domain='e2b.app' on create/list despite ambient E2B_DOMAIN and fails closed on E2B_DEBUG=true."""
    create_calls: list[dict[str, object]] = []
    list_calls: list[dict[str, object]] = []

    class _RecordingPaginator:
        def next_items(self) -> list[object]:
            return []

    class _RecordingSandbox(_FakeSandbox):
        @classmethod
        def create(cls, **kwargs: object) -> _RecordingSandbox:
            create_calls.append(dict(kwargs))
            return cls()

        @staticmethod
        def list(**kwargs: object) -> _RecordingPaginator:
            list_calls.append(dict(kwargs))
            return _RecordingPaginator()

    monkeypatch.setattr(e2b_code_interpreter, "Sandbox", _RecordingSandbox)
    monkeypatch.setenv("E2B_DOMAIN", "evil.example")
    monkeypatch.setenv("E2B_DEBUG", "false")

    tool = MindRoomE2BTools(api_key="scoped-e2b-key", timeout=120)
    assert isinstance(tool.sandbox, _RecordingSandbox)
    assert create_calls == [{"api_key": "scoped-e2b-key", "timeout": 120, "domain": "e2b.app", "debug": False}]
    assert json.loads(tool.e2b_inspect("list_sandboxes")) == {"status": "success", "sandboxes": []}
    assert list_calls == [{"api_key": "scoped-e2b-key", "domain": "e2b.app", "debug": False}]
    real_cfg = ConnectionConfig(api_key="scoped-e2b-key", domain="e2b.app", debug=False)
    assert real_cfg.api_url == "https://api.e2b.app"

    # Ambient E2B_DEBUG=true fails closed before Sandbox.create or Sandbox.list runs.
    monkeypatch.setenv("E2B_DEBUG", "true")
    create_calls.clear()
    list_calls.clear()
    fresh = MindRoomE2BTools(api_key="scoped-e2b-key")
    with pytest.raises(ValueError, match="E2B_DEBUG"):
        _ = fresh.sandbox
    assert create_calls == []
    list_err = json.loads(fresh.e2b_inspect("list_sandboxes"))
    assert list_err["status"] == "error"
    assert "E2B_DEBUG" in list_err["message"]
    assert list_calls == []
