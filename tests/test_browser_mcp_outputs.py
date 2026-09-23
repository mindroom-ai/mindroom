"""Native output paths are confined before MCP startup or dispatch."""

import asyncio
import base64
import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from agno.tools.function import ToolResult
from mcp import StdioServerParameters
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from mindroom.playwright_mcp_session import PlaywrightMCPSession
from mindroom.worker_computer import mcp_provider
from mindroom.worker_computer.mcp_catalog import browser_mcp_catalog
from mindroom.worker_computer.mcp_provider import WorkerBrowserMCP

_OUTPUT_FUNCTIONS = [
    "browser_console_messages",
    "browser_evaluate",
    "browser_network_requests",
    "browser_network_request",
    "browser_pdf_save",
    "browser_take_screenshot",
    "browser_snapshot",
]
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
)


@pytest.mark.asyncio
async def test_inline_screenshot_is_prepared_and_persisted_once(tmp_path: Path) -> None:
    """Inline MCP pixels use bounded delivery and retain the exact captured artifact."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    session = AsyncMock(spec=PlaywrightMCPSession)
    session.call_tool.return_value = CallToolResult(
        content=[
            TextContent(type="text", text="Screenshot captured"),
            ImageContent(type="image", data=base64.b64encode(PNG_BYTES).decode(), mimeType="image/png"),
        ],
    )
    provider._session = session
    provider._ready = True

    result = await provider.execute("browser_take_screenshot", {"type": "png", "scale": "css"})

    assert result.images
    assert result.images[0].content == PNG_BYTES
    receipt = json.loads(result.content)
    assert receipt["result"] == "Screenshot captured"
    assert receipt["view_status"] == "ready"
    saved_path = workspace / receipt["path"]
    assert saved_path.parent == workspace / "browser"
    assert saved_path.read_bytes() == PNG_BYTES
    session.call_tool.assert_awaited_once_with("browser_take_screenshot", {"type": "png", "scale": "css"})


@pytest.mark.asyncio
async def test_inline_screenshot_preparation_does_not_block_browser_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem persistence and bounded image decoding run away from browser control."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    session = AsyncMock(spec=PlaywrightMCPSession)
    session.call_tool.return_value = CallToolResult(
        content=[ImageContent(type="image", data=base64.b64encode(PNG_BYTES).decode(), mimeType="image/png")],
    )
    provider._session = session
    provider._ready = True
    preparation_started = threading.Event()
    release_preparation = threading.Event()

    original_image_result = mcp_provider.image_result

    def slow_image_result(data: bytes, *, metadata: dict[str, object]) -> ToolResult:
        preparation_started.set()
        release_preparation.wait(timeout=1)
        return original_image_result(data, metadata=metadata)

    monkeypatch.setattr(mcp_provider, "image_result", slow_image_result)
    release_timer = threading.Timer(0.2, release_preparation.set)
    release_timer.start()
    try:
        task = asyncio.create_task(provider.execute("browser_take_screenshot", {"type": "png", "scale": "css"}))
        await asyncio.sleep(0.05)

        assert preparation_started.is_set()
        assert not release_preparation.is_set()
        assert not task.done()
        release_preparation.set()
        await task
    finally:
        release_preparation.set()
        release_timer.join(timeout=1)


@pytest.mark.asyncio
async def test_inline_screenshot_is_created_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retained pixels are private from the file creation syscall onward."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    session = AsyncMock(spec=PlaywrightMCPSession)
    session.call_tool.return_value = CallToolResult(
        content=[ImageContent(type="image", data=base64.b64encode(PNG_BYTES).decode(), mimeType="image/png")],
    )
    provider._session = session
    provider._ready = True
    creation_modes: list[int] = []
    original_open = os.open

    def tracked_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if isinstance(path, str) and path.startswith("page-"):
            creation_modes.append(mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", tracked_open)

    result = await provider.execute("browser_take_screenshot", {"type": "png", "scale": "css"})

    assert result.images
    assert creation_modes == [0o600]


@pytest.mark.asyncio
async def test_inline_screenshot_rejects_output_directory_swap(tmp_path: Path) -> None:
    """A post-validation symlink swap cannot redirect retained pixels outside the workspace."""
    workspace = tmp_path / "workspace"
    output = workspace / "browser"
    output.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    session = AsyncMock(spec=PlaywrightMCPSession)

    async def swap_output(_name: str, _arguments: dict[str, object]) -> CallToolResult:
        output.rmdir()
        output.symlink_to(outside, target_is_directory=True)
        return CallToolResult(
            content=[ImageContent(type="image", data=base64.b64encode(PNG_BYTES).decode(), mimeType="image/png")],
        )

    session.call_tool.side_effect = swap_output
    provider._session = session
    provider._ready = True

    with pytest.raises(OSError, match="output directory"):
        await provider.execute("browser_take_screenshot", {"type": "png", "scale": "css"})

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_explicit_screenshot_filename_remains_save_only(tmp_path: Path) -> None:
    """A caller-provided filename keeps upstream save-only behavior without inline media."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    session = AsyncMock(spec=PlaywrightMCPSession)
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="Screenshot saved")],
    )
    provider._session = session
    provider._ready = True

    result = await provider.execute("browser_take_screenshot", {"filename": "captures/page.png"})

    assert result.content == "Screenshot saved"
    assert not result.images
    session.call_tool.assert_awaited_once_with(
        "browser_take_screenshot",
        {"filename": str(workspace / "captures" / "page.png")},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("function", _OUTPUT_FUNCTIONS)
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        "absolute",
        "traversal",
        "file_link",
        "directory_link",
        "dangling_link",
        "loop",
        "directory",
        "fifo",
        "empty",
        "null",
        "number",
        "array",
        "nul",
    ],
)
async def test_invalid_output_never_starts_or_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    function: str,
    running: bool,
    case: str,
) -> None:
    """All admitted filenames reject invalid targets without retiring a valid session."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside canary")
    (workspace / "file-link").symlink_to(outside)
    (workspace / "directory-link").symlink_to(tmp_path, target_is_directory=True)
    (workspace / "dangling-link").symlink_to(tmp_path / "missing.txt")
    (workspace / "loop").symlink_to(workspace / "loop")
    os.mkfifo(workspace / "fifo")
    filename = {
        "absolute": str(outside),
        "traversal": "../outside.txt",
        "file_link": "file-link",
        "directory_link": "directory-link/new.txt",
        "dangling_link": "dangling-link",
        "loop": "loop/new.txt",
        "directory": ".",
        "fifo": "fifo",
        "empty": "",
        "null": None,
        "number": 1,
        "array": ["inside.txt"],
        "nul": "bad\x00path",
    }[case]
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    start = AsyncMock(side_effect=AssertionError("Invalid output started browser resources"))
    monkeypatch.setattr(provider._verifier, "start", start)
    session = AsyncMock(spec=PlaywrightMCPSession)
    if running:
        provider._session = session
        provider._ready = True
    arguments: dict[str, object] = {"filename": filename}
    original = deepcopy(arguments)

    with pytest.raises(ValueError, match=r"filename|output|workspace"):
        await provider.execute(function, arguments)

    start.assert_not_awaited()
    session.call_tool.assert_not_awaited()
    session.close.assert_not_awaited()
    assert provider._session is (session if running else None)
    assert provider._ready is running
    assert not (tmp_path / "storage").exists()
    assert outside.read_text() == "outside canary"
    assert not (tmp_path / "missing.txt").exists()
    assert arguments == original


@pytest.mark.asyncio
@pytest.mark.parametrize("function", _OUTPUT_FUNCTIONS)
@pytest.mark.parametrize(
    "case",
    ["relative", "absolute", "file_link", "directory_link", "new_parent", "dangling_link", "browser_output"],
)
async def test_safe_named_outputs_are_canonical(
    tmp_path: Path,
    function: str,
    case: str,
) -> None:
    """Safe internal symlinks and new files retain native workspace-relative semantics."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("inside")
    (workspace / "safe-file").symlink_to(inside)
    (workspace / "safe-directory").symlink_to(workspace, target_is_directory=True)
    (workspace / "safe-dangling").symlink_to(workspace / "new.txt")
    filename, expected = {
        "relative": ("new.txt", workspace / "new.txt"),
        "absolute": (str(inside), inside),
        "file_link": ("safe-file", inside),
        "directory_link": ("safe-directory/new.txt", workspace / "new.txt"),
        "new_parent": ("new-parent/new.txt", workspace / "new-parent/new.txt"),
        "dangling_link": ("safe-dangling", workspace / "new.txt"),
        "browser_output": ("browser/native.png", workspace / "browser/native.png"),
    }[case]
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    session = AsyncMock(spec=PlaywrightMCPSession)
    session.call_tool.return_value = CallToolResult(content=[])
    provider._session = session
    provider._ready = True
    arguments: dict[str, object] = {"filename": filename}

    await provider.execute(function, arguments)

    session.call_tool.assert_awaited_once_with(function, {"filename": str(expected)})
    assert arguments == {"filename": filename}


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("case", ["outside", "loop", "file"])
async def test_invalid_automatic_output_root_blocks_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running: bool,
    case: str,
) -> None:
    """Recheck automatic artifacts/downloads even after a native session is established."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = workspace / "browser"
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("outside canary")
    session = AsyncMock(spec=PlaywrightMCPSession)
    session.list_tools.return_value = tuple(Tool.model_validate(tool) for tool in browser_mcp_catalog().values())
    session.call_tool.return_value = CallToolResult(content=[])
    monkeypatch.setattr("mindroom.worker_computer.mcp_provider.PlaywrightMCPSession", lambda _parameters: session)
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    try:
        if running:
            await provider.execute("browser_tabs", {"action": "list"})
            output.rmdir()
            session.reset_mock()
        if case == "outside":
            output.symlink_to(outside, target_is_directory=True)
        elif case == "loop":
            output.symlink_to(output)
        else:
            output.write_text("not a directory")
        start = AsyncMock(side_effect=AssertionError("Invalid output root started browser resources"))
        monkeypatch.setattr(provider._verifier, "start", start)

        with pytest.raises(ValueError, match=r"output|workspace"):
            await provider.execute("browser_snapshot", {})

        start.assert_not_awaited()
        session.call_tool.assert_not_awaited()
        session.close.assert_not_awaited()
        assert provider._ready is running
        assert canary.read_text() == "outside canary"
        assert list(outside.iterdir()) == [canary]
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("case", ["outside_file", "outside_directory", "dangling_outside", "loop", "special_file"])
async def test_invalid_automatic_output_child_blocks_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running: bool,
    case: str,
) -> None:
    """A direct download target link is checked even when the call has no filename."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = workspace / "browser"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("outside canary")
    os.mkfifo(workspace / "fifo")
    session = AsyncMock(spec=PlaywrightMCPSession)
    session.list_tools.return_value = tuple(Tool.model_validate(tool) for tool in browser_mcp_catalog().values())
    session.call_tool.return_value = CallToolResult(content=[])
    monkeypatch.setattr("mindroom.worker_computer.mcp_provider.PlaywrightMCPSession", lambda _parameters: session)
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    try:
        if running:
            await provider.execute("browser_tabs", {"action": "list"})
            session.reset_mock()
        link = output / "download.txt"
        target = {
            "outside_file": canary,
            "outside_directory": outside,
            "dangling_outside": outside / "missing.txt",
            "loop": link,
            "special_file": workspace / "fifo",
        }[case]
        link.symlink_to(target)
        start = AsyncMock(side_effect=AssertionError("Invalid download target started browser resources"))
        monkeypatch.setattr(provider._verifier, "start", start)

        with pytest.raises(ValueError, match=r"output|workspace"):
            await provider.execute("browser_navigate", {"url": "https://example.org/download"})

        start.assert_not_awaited()
        session.call_tool.assert_not_awaited()
        session.close.assert_not_awaited()
        assert provider._session is (session if running else None)
        assert provider._ready is running
        assert canary.read_text() == "outside canary"
        assert list(outside.iterdir()) == [canary]
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["file_link", "dangling_link", "directory_link", "directory", "missing_root"])
async def test_safe_automatic_output_children_preserve_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Internal links stay supported, and direct admission never walks child directories."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = workspace / "browser"
    inside = workspace / "inside.txt"
    inside.write_text("inside")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside canary")
    if case != "missing_root":
        output.mkdir()
        child = output / "download.txt"
        if case == "file_link":
            child.symlink_to(inside)
        elif case == "dangling_link":
            child.symlink_to(workspace / "missing.txt")
        else:
            directory = workspace / "reports" if case == "directory_link" else child
            directory.mkdir()
            (directory / "nested-link.txt").symlink_to(outside)
            if case == "directory_link":
                child.symlink_to(directory, target_is_directory=True)
    session = AsyncMock(spec=PlaywrightMCPSession)
    session.list_tools.return_value = tuple(Tool.model_validate(tool) for tool in browser_mcp_catalog().values())
    session.call_tool.return_value = CallToolResult(content=[])
    monkeypatch.setattr("mindroom.worker_computer.mcp_provider.PlaywrightMCPSession", lambda _parameters: session)
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    try:
        for _ in range(2):
            await provider.execute("browser_navigate", {"url": "https://example.org/download"})
            session.call_tool.assert_awaited_with("browser_navigate", {"url": "https://example.org/download"})
        assert output.is_dir()
        assert provider._ready
        assert inside.read_text() == "inside"
        assert outside.read_text() == "outside canary"
        assert not (workspace / "missing.txt").exists()
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("error", [PermissionError("fixture permission denied"), OSError("fixture enumeration failed")])
async def test_automatic_output_enumeration_errors_preserve_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running: bool,
    error: OSError,
) -> None:
    """Filesystem admission errors retain the ValueError contract before dispatch."""
    workspace = tmp_path / "workspace"
    output = workspace / "browser"
    output.mkdir(parents=True)
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    session = AsyncMock(spec=PlaywrightMCPSession)
    if running:
        provider._session = session
        provider._ready = True
    start = AsyncMock(side_effect=AssertionError("Unreadable outputs started browser resources"))
    monkeypatch.setattr(provider._verifier, "start", start)
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", Mock(side_effect=error))
        with pytest.raises(ValueError, match=r"output|workspace"):
            await provider.execute("browser_snapshot", {})

    start.assert_not_awaited()
    session.call_tool.assert_not_awaited()
    session.close.assert_not_awaited()
    assert provider._session is (session if running else None)
    assert provider._ready is running


@pytest.mark.asyncio
async def test_safe_automatic_output_root_preserves_native_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An internal directory symlink supports startup and default artifacts/downloads."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = workspace / "artifacts"
    artifacts.mkdir()
    (workspace / "browser").symlink_to(artifacts, target_is_directory=True)
    session = AsyncMock(spec=PlaywrightMCPSession)
    session.list_tools.return_value = tuple(Tool.model_validate(tool) for tool in browser_mcp_catalog().values())
    session.call_tool.return_value = CallToolResult(content=[])
    launches: list[StdioServerParameters] = []

    def launch(parameters: StdioServerParameters) -> PlaywrightMCPSession:
        launches.append(parameters)
        return session

    monkeypatch.setattr("mindroom.worker_computer.mcp_provider.PlaywrightMCPSession", launch)
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    try:
        for function in _OUTPUT_FUNCTIONS:
            await provider.execute(function, {})
            session.call_tool.assert_awaited_with(function, {})
        assert len(launches) == 1
        parameters = launches[0]
        assert Path(parameters.args[parameters.args.index("--output-dir") + 1]).resolve() == artifacts
        assert provider._ready
    finally:
        await provider.close()
