"""Tests for E2B local file transfers confined to the agent workspace."""

from __future__ import annotations

import base64
import json
import os
import struct
import tempfile
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import e2b.api.client_sync as e2b_client_sync
import pytest
from e2b_code_interpreter import Sandbox
from e2b_code_interpreter.models import Execution, Result
from pyqwest import ConnectTimeout, Headers, ReadError, SyncRequest, SyncResponse

import mindroom.custom_tools.e2b as e2b_module
from mindroom.credentials.manager import CredentialsManager
from mindroom.custom_tools.e2b import MindRoomE2BTools
from mindroom.path_confinement import MAX_READ_BYTES
from mindroom.tool_system.agno_toolkits import ToolCallActor
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
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


_SANDBOX_HOST = "sandbox.e2b.app"
_SANDBOX_ID = "sandbox-1"
_ENVD_PORT = "49983"
_JUPYTER_PORT = "49999"
_ENVD_TOKEN = "envd-token"  # noqa: S105
_CHUNK = 1 << 20


def _actor() -> ToolCallActor:
    """Return a fresh stand-in for the calling agent the loop injects; no E2B tool reads it."""
    return ToolCallActor(session_state={})


def _json_response(status: int, payload: object) -> SyncResponse:
    return SyncResponse(
        status=status,
        headers=Headers({"content-type": "application/json"}),
        content=json.dumps(payload).encode(),
    )


def _connect_envelope(message: object, *, end_stream: bool = False) -> bytes:
    """Frame one Connect streaming message: a flags byte, a big-endian length, and the JSON payload."""
    payload = json.dumps(message).encode()
    return struct.pack(">BI", 0x02 if end_stream else 0, len(payload)) + payload


@dataclass
class _FakeFiles:
    stored: dict[str, bytes] = field(default_factory=dict)
    sparse: dict[str, int] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)
    served: int = 0

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
    stdout_bytes: int = 0
    served: int = 0

    def events(self) -> Iterator[bytes]:
        """Stream envd's process events for one command, as a server-streaming Connect response."""
        yield _connect_envelope({"event": {"start": {"pid": 1}}})
        for offset in range(0, self.stdout_bytes, _CHUNK):
            chunk = b"x" * min(_CHUNK, self.stdout_bytes - offset)
            self.served += len(chunk)
            yield _connect_envelope({"event": {"data": {"stdout": base64.b64encode(chunk).decode()}}})
        yield _connect_envelope({"event": {"end": {"exitCode": 0, "exited": True}}})
        yield _connect_envelope({}, end_stream=True)


@dataclass
class _FakeE2B:
    """Answer the E2B API, envd, and Jupyter on the wire, in place of the SDK's reqwest transport.

    Every SDK stack (REST client, envd HTTP API, envd Connect RPC, the Jupyter client)
    sends its requests through this one transport, so the real SDK builds and parses them.
    """

    files: _FakeFiles = field(default_factory=_FakeFiles)
    commands: _FakeCommands = field(default_factory=_FakeCommands)
    cell_output: Callable[[str], Iterator[bytes]] = lambda _code: iter(())
    cell_failure: BaseException | None = None
    running: bool = True

    def execute_sync(self, request: SyncRequest) -> SyncResponse:
        url = urlsplit(request.url)
        if (request.method, url.netloc, url.path) == ("POST", "api.e2b.app", "/v2/sandboxes"):
            assert request.headers["x-api-key"] == "test"
            sandbox = {"templateID": "code-interpreter-v1", "sandboxID": _SANDBOX_ID, "clientID": "client"}
            return _json_response(201, {**sandbox, "envdVersion": "0.6.7", "envdAccessToken": _ENVD_TOKEN})
        assert url.netloc == _SANDBOX_HOST
        assert request.headers["e2b-sandbox-id"] == _SANDBOX_ID
        assert request.headers["x-access-token"] == _ENVD_TOKEN
        if request.headers["e2b-sandbox-port"] == _JUPYTER_PORT:
            return self._execute(request, url.path)
        assert request.headers["e2b-sandbox-port"] == _ENVD_PORT
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        match request.method, url.path:
            case "GET", "/files":
                return self._read(query["path"])
            case "POST", "/files":
                return self._write(request, query["path"])
            case "POST", "/process.Process/Start":
                assert request.headers["content-type"] == "application/connect+json"
                headers = Headers({"content-type": "application/connect+json"})
                return SyncResponse(status=200, headers=headers, content=self.commands.events())
            case "GET", "/health":
                return SyncResponse(status=204 if self.running else 502, content=b"")
        msg = f"unexpected request {request.method} {request.url}"
        raise AssertionError(msg)

    def _read(self, path: str) -> SyncResponse:
        self.files.reads.append(path)
        if path not in self.files.stored and path not in self.files.sparse:
            return _json_response(404, {"code": 404, "message": f"path '{path}' does not exist"})
        headers = Headers({"content-type": "application/octet-stream"})
        return SyncResponse(status=200, headers=headers, content=self.files.chunks(path))

    def _write(self, request: SyncRequest, path: str) -> SyncResponse:
        content = request.content
        body = content if isinstance(content, bytes) else b"".join(content)
        head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
        (part,) = BytesParser(policy=HTTP).parsebytes(head + body).iter_parts()
        self.files.stored[path] = part.get_payload(decode=True)
        return _json_response(200, [{"name": path.rsplit("/", 1)[-1], "type": "file", "path": f"/home/user/{path}"}])

    def _execute(self, request: SyncRequest, path: str) -> SyncResponse:
        assert (request.method, path) == ("POST", "/execute")
        if self.cell_failure is not None:
            raise self.cell_failure
        content = request.content
        code = json.loads(content if isinstance(content, bytes) else b"".join(content))["code"]
        headers = Headers({"content-type": "application/x-ndjson"})
        return SyncResponse(status=200, headers=headers, content=self.cell_output(code))


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


def _install_fake_e2b(monkeypatch: pytest.MonkeyPatch) -> _FakeE2B:
    """Route every request the E2B SDK sends to a fresh in-memory E2B service."""
    service = _FakeE2B()
    monkeypatch.setattr(e2b_client_sync, "SyncHTTPTransport", lambda **_options: service)
    for cache in ("_transports", "_pools", "_httpx_transports"):
        monkeypatch.setattr(e2b_client_sync, cache, {})
    return service


@pytest.fixture
def e2b_service(monkeypatch: pytest.MonkeyPatch) -> _FakeE2B:
    """Route every request the E2B SDK sends to an in-memory E2B service."""
    return _install_fake_e2b(monkeypatch)


@pytest.fixture
def make_tool(
    e2b_service: _FakeE2B,  # noqa: ARG001  # routes the SDK to the fake service
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Callable[[Path | None], MindRoomE2BTools]:
    """Build toolkits backed by a real SDK sandbox talking to the fake service."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    def build(workspace_root: Path | None) -> MindRoomE2BTools:
        tool = MindRoomE2BTools(api_key="test", tool_output_workspace_root=workspace_root)
        assert isinstance(tool.sandbox, Sandbox)
        return tool

    return build


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
    e2b_service: _FakeE2B,
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
    upload = tool.functions["upload_file"].entrypoint
    assert isinstance(tool, MindRoomE2BTools)
    assert upload is not None

    assert upload("report.csv") == "/home/user/report.csv"
    assert "Error uploading file" in _error(upload(str(outside / "secret.env")))
    assert e2b_service.files.stored == {"report.csv": b"a,b\n"}


def test_upload_reads_workspace_relative_file(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
) -> None:
    """Uploads read workspace-relative files."""
    (workspace / "data").mkdir()
    (workspace / "data" / "report.csv").write_bytes(b"a,b\n")
    tool = make_tool(workspace)

    assert tool.upload_file("data/report.csv", "workspace/report.csv") == "/home/user/workspace/report.csv"
    assert e2b_service.files.stored == {"workspace/report.csv": b"a,b\n"}


def test_upload_follows_links_that_stay_inside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
) -> None:
    """Uploads follow symlinks whose targets stay inside the workspace."""
    (workspace / "docs").mkdir()
    (workspace / "docs" / "notes.md").write_bytes(b"notes")
    (workspace / "knowledge").symlink_to(workspace / "docs")
    tool = make_tool(workspace)

    assert tool.upload_file("knowledge/notes.md") == "/home/user/notes.md"
    assert e2b_service.files.stored == {"notes.md": b"notes"}


@pytest.mark.parametrize(
    "requested",
    ["absolute", "../outside/secret.env", "leak.env", "linked/secret.env", "loop", "root_link", "/", "..", ""],
)
def test_upload_rejects_paths_outside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
    outside: Path,
    requested: str,
) -> None:
    """Uploads never read absolute, parent-escaping, outward-linked, or looping paths."""
    _plant_escape_links(workspace, outside)
    tool = make_tool(workspace)

    assert "Error uploading file" in _error(tool.upload_file(_local_path(requested, outside), "/tmp/x"))  # noqa: S108
    assert e2b_service.files.stored == {}


@pytest.mark.parametrize("name", ["folder", "pipe"])
def test_upload_rejects_non_regular_files(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
    name: str,
) -> None:
    """Uploads only read regular files and never block on a FIFO."""
    (workspace / "folder").mkdir()
    os.mkfifo(workspace / "pipe")
    tool = make_tool(workspace)

    assert "regular file" in _error(tool.upload_file(name))
    assert e2b_service.files.stored == {}


def test_upload_refuses_oversized_file_before_reading(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
) -> None:
    """A sparse workspace file above the shared read limit is refused instead of buffered whole."""
    with (workspace / "huge.bin").open("wb") as file:
        file.truncate(MAX_READ_BYTES + 1)
    tool = make_tool(workspace)

    assert "size limit" in _error(tool.upload_file("huge.bin"))
    assert e2b_service.files.stored == {}


def test_upload_rejects_file_swapped_to_link_after_resolution(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
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
    assert e2b_service.files.stored == {}


def test_transfers_require_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
) -> None:
    """Agents without a workspace cannot transfer local files."""
    tool = make_tool(None)
    e2b_service.files.stored["/tmp/x"] = b"payload"  # noqa: S108

    assert "requires an agent workspace" in _error(tool.upload_file("report.csv"))
    assert "require an agent workspace" in _error(tool.download_file_from_sandbox("/tmp/x"))  # noqa: S108
    assert e2b_service.files.reads == []


def test_download_writes_inside_workspace(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Downloads land in the workspace, never the process working directory."""
    monkeypatch.chdir(tmp_path)
    tool = make_tool(workspace)
    e2b_service.files.stored["/tmp/out/result.csv"] = b"x,y\n"  # noqa: S108

    assert tool.download_file_from_sandbox("/tmp/out/result.csv") == "result.csv"  # noqa: S108
    assert tool.download_file_from_sandbox("/tmp/out/result.csv", "exports/final.csv") == "exports/final.csv"  # noqa: S108
    assert (workspace / "result.csv").read_bytes() == b"x,y\n"
    assert (workspace / "exports" / "final.csv").read_bytes() == b"x,y\n"
    assert not (tmp_path / "result.csv").exists()


def test_download_streams_file_in_chunks(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
) -> None:
    """A sandbox file below the limit arrives whole even when it spans many chunks."""
    tool = make_tool(workspace)
    e2b_service.files.sparse["/tmp/data.bin"] = 3 * _CHUNK + 5  # noqa: S108

    assert tool.download_file_from_sandbox("/tmp/data.bin") == "data.bin"  # noqa: S108
    assert (workspace / "data.bin").stat().st_size == 3 * _CHUNK + 5


def test_download_refuses_oversized_file_while_streaming(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
) -> None:
    """A huge sandbox file is refused once it passes the limit, without buffering it or publishing a partial file."""
    (workspace / "result.bin").write_bytes(b"previous")
    tool = make_tool(workspace)
    e2b_service.files.sparse["/tmp/huge.bin"] = 8 << 30  # noqa: S108

    message = _error(tool.download_file_from_sandbox("/tmp/huge.bin", "result.bin"))  # noqa: S108

    assert "64 MiB transfer limit" in message
    assert e2b_service.files.served <= MAX_READ_BYTES + _CHUNK
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
    e2b_service: _FakeE2B,
    workspace: Path,
) -> None:
    """File reads return the decoded sandbox file."""
    tool = make_tool(workspace)
    e2b_service.files.stored["/tmp/notes.txt"] = "caf\u00e9\n".encode()  # noqa: S108

    assert tool.read_file_content("/tmp/notes.txt") == "caf\u00e9\n"  # noqa: S108
    assert tool.read_file_content("/tmp/notes.txt", encoding="latin-1") == "caf\u00c3\u00a9\n"  # noqa: S108


def test_read_file_content_refuses_oversized_file_while_streaming(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
) -> None:
    """A huge sandbox file read is refused once it passes the limit instead of being buffered whole."""
    tool = make_tool(workspace)
    e2b_service.files.sparse["/tmp/huge.log"] = 8 << 30  # noqa: S108

    assert "64 MiB transfer limit" in _error(tool.read_file_content("/tmp/huge.log"))  # noqa: S108
    assert e2b_service.files.served <= MAX_READ_BYTES + _CHUNK


def test_run_command_refuses_output_past_the_limit_while_streaming(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
) -> None:
    """A command's output is refused once it passes the limit, before the SDK can keep the rest."""
    tool = make_tool(workspace)
    commands = e2b_service.commands
    commands.stdout_bytes = 5

    assert json.loads(tool.run_command("printf xxxxx")) == ["STDOUT:\nxxxxx"]

    commands.stdout_bytes = MAX_READ_BYTES + 4 * _CHUNK
    commands.served = 0
    assert "64 MiB transfer limit" in _error(tool.run_command("yes"))
    assert commands.served <= MAX_READ_BYTES + _CHUNK


def test_run_python_code_refuses_output_past_the_limit_while_streaming(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
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

    e2b_service.cell_output = cell_output

    assert json.loads(tool.run_python_code("small()")) == [
        "Logs:\nLogs(stdout: ['hi\\n'], stderr: [])",
        "Result 1: 2",
    ]
    previous = tool.last_execution

    assert "64 MiB transfer limit" in _error(tool.run_python_code("huge()"))
    assert served <= MAX_READ_BYTES + _CHUNK
    assert tool.last_execution is previous


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (TimeoutError("timed out"), "Execution timed out"),
        (ConnectTimeout("timed out"), "Request timed out"),
    ],
)
def test_run_python_code_reports_timeouts_like_the_sdk(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    message: str,
) -> None:
    """A cell that stays silent too long, or a sandbox that cannot be reached, reports the SDK's timeout message."""
    monkeypatch.setattr(e2b_client_sync, "connection_retries", 0)  # skip the SDK's connect retry backoff
    tool = make_tool(workspace)
    e2b_service.cell_failure = failure

    assert message in _error(tool.run_python_code("train()"))


@pytest.mark.parametrize(("running", "message"), [(False, "The sandbox was killed"), (True, "connection reset")])
def test_run_python_code_reports_dropped_connections_like_the_sdk(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
    running: bool,
    message: str,
) -> None:
    """A connection dropped mid-cell reports a killed sandbox when its health check says so, else the read error."""
    tool = make_tool(workspace)

    def cell_output(_code: str) -> Iterator[bytes]:
        yield b'{"type": "stdout", "text": "hi\\n", "timestamp": "2026-10-05T00:00:00Z"}\n'
        e2b_service.running = running
        msg = "connection reset"
        raise ReadError(msg)

    e2b_service.cell_output = cell_output

    assert message in _error(tool.run_python_code("train()"))


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
    e2b_service: _FakeE2B,
    workspace: Path,
    outside: Path,
    requested: str,
) -> None:
    """Downloads never write absolute, parent-escaping, outward-linked, or looping paths."""
    _plant_escape_links(workspace, outside)
    tool = make_tool(workspace)
    e2b_service.files.stored["/tmp/evil.py"] = b"import os"  # noqa: S108
    path = _local_path(requested, outside)

    assert "Error downloading file" in _error(tool.download_file_from_sandbox("/tmp/evil.py", path))  # noqa: S108
    assert e2b_service.files.reads == []
    assert (outside / "secret.env").read_text(encoding="utf-8") == "TOKEN=secret"
    assert sorted(entry.name for entry in outside.iterdir()) == ["secret.env"]


def test_download_rejects_parent_swapped_to_link_after_resolution(
    make_tool: Callable[[Path | None], MindRoomE2BTools],
    e2b_service: _FakeE2B,
    workspace: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent swapped for an outward link after resolution cannot redirect a download."""
    (workspace / "plugins").mkdir()
    tool = make_tool(workspace)
    e2b_service.files.stored["/tmp/evil.py"] = b"import os"  # noqa: S108
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
    e2b_service: _FakeE2B,
    workspace: Path,
    outside: Path,
    tmp_path: Path,
    local_path: str,
) -> None:
    """A workspace that worker code replaced with a link after the toolkit was built never redirects a download."""
    tool = make_tool(workspace)
    e2b_service.files.stored["/tmp/out/result.csv"] = b"x,y\n"  # noqa: S108
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

    _plant_escape_links(workspace, outside)

    saved = tool.download_png_result(_actor(), 0, "charts/plot.png")
    rejected = tool.download_png_result(_actor(), 0, "linked/plot.png")

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

    result = tool.download_chart_data(_actor())

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

    result = tool.download_chart_data(_actor(), 0, _local_path(requested, outside), add_as_artifact=False)

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

    assert tool.download_chart_data(_actor(), -5).content.startswith("Error extracting chart data")
    assert list(workspace.iterdir()) == []
