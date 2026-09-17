"""Native file inputs stay within the assigned workspace before MCP dispatch."""

import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult

from mindroom.playwright_mcp_session import PlaywrightMCPSession
from mindroom.worker_computer.mcp_provider import WorkerBrowserMCP


@pytest.mark.asyncio
@pytest.mark.parametrize("function", ["browser_file_upload", "browser_drop"])
@pytest.mark.parametrize(
    "case",
    [
        "absolute",
        "traversal",
        "file_link",
        "directory_link",
        "missing",
        "directory",
        "mixed",
        "null",
        "string",
        "mapping",
        "number",
        "null_item",
        "empty_item",
        "nul",
        "loop",
        "fifo",
    ],
)
async def test_invalid_file_inputs_never_start_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    function: str,
    case: str,
) -> None:
    """Resolve the whole input array before any browser, verifier or proxy starts."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("inside")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside canary")
    (workspace / "file-link").symlink_to(outside)
    (workspace / "directory-link").symlink_to(tmp_path, target_is_directory=True)
    (workspace / "loop").symlink_to(workspace / "loop")
    os.mkfifo(workspace / "fifo")
    paths: object = {
        "absolute": [str(outside)],
        "traversal": ["../outside.txt"],
        "file_link": ["file-link"],
        "directory_link": ["directory-link/outside.txt"],
        "missing": ["missing.txt"],
        "directory": ["."],
        "mixed": [str(inside), str(outside)],
        "null": None,
        "string": str(inside),
        "mapping": {"file": str(inside)},
        "number": [1],
        "null_item": [None],
        "empty_item": [""],
        "nul": ["bad\x00path"],
        "loop": ["loop"],
        "fifo": ["fifo"],
    }[case]
    arguments = {"paths": paths, "target": "#drop"} if function == "browser_drop" else {"paths": paths}
    original = deepcopy(arguments)
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    start = AsyncMock(side_effect=AssertionError("Invalid file input started the browser boundary"))
    monkeypatch.setattr(provider._verifier, "start", start)

    with pytest.raises(ValueError, match=r"paths|workspace|file"):
        await provider.execute(function, arguments)

    start.assert_not_awaited()
    assert provider._session is None
    assert not (tmp_path / "storage").exists()
    assert arguments == original


@pytest.mark.asyncio
@pytest.mark.parametrize("function", ["browser_file_upload", "browser_drop"])
async def test_invalid_files_preserve_the_running_session(tmp_path: Path, function: str) -> None:
    """Reject the complete mixed array without dispatching or closing a live browser."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside canary")
    provider = WorkerBrowserMCP(display=":99", workspace=workspace, storage_root=tmp_path / "storage")
    session = AsyncMock(spec=PlaywrightMCPSession)
    provider._session = session
    provider._ready = True

    with pytest.raises(ValueError, match="workspace"):
        await provider.execute(function, {"paths": ["inside.txt", str(outside)], "target": "#drop"})

    session.call_tool.assert_not_awaited()
    session.close.assert_not_awaited()
    assert provider._session is session
    assert provider._ready


@pytest.mark.asyncio
@pytest.mark.parametrize("function", ["browser_file_upload", "browser_drop"])
async def test_allowed_files_are_canonical_without_mutating_arguments(tmp_path: Path, function: str) -> None:
    """Relative files and safe symlinks resolve to regular files in the canonical workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("inside")
    (workspace / "safe-link").symlink_to(inside)
    workspace_alias = tmp_path / "workspace-alias"
    workspace_alias.symlink_to(workspace, target_is_directory=True)
    provider = WorkerBrowserMCP(display=":99", workspace=workspace_alias, storage_root=tmp_path / "storage")
    session = AsyncMock(spec=PlaywrightMCPSession)
    session.call_tool.return_value = CallToolResult(content=[])
    provider._session = session
    provider._ready = True
    arguments: dict[str, object] = {"paths": ["inside.txt", "safe-link", str(workspace_alias / "inside.txt")]}
    if function == "browser_drop":
        arguments["target"] = "#drop"
        arguments["data"] = {"text/plain": "retained data"}
    original = deepcopy(arguments)

    await provider.execute(function, arguments)

    expected = {**arguments, "paths": [str(inside)] * 3}
    session.call_tool.assert_awaited_once_with(function, expected)
    assert arguments == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function", "arguments"),
    [
        ("browser_file_upload", {}),
        ("browser_file_upload", {"paths": []}),
        ("browser_drop", {"target": "#drop", "data": {"text/plain": "drop data"}}),
        ("browser_drop", {"target": "#drop", "paths": []}),
    ],
)
async def test_cancel_empty_and_data_only_calls_keep_native_semantics(
    tmp_path: Path,
    function: str,
    arguments: dict[str, object],
) -> None:
    """Omitted/empty file arrays do not become mandatory file inputs."""
    provider = WorkerBrowserMCP(display=":99", workspace=tmp_path, storage_root=tmp_path / "storage")
    session = AsyncMock(spec=PlaywrightMCPSession)
    session.call_tool.return_value = CallToolResult(content=[])
    provider._session = session
    provider._ready = True
    original = deepcopy(arguments)

    await provider.execute(function, arguments)

    session.call_tool.assert_awaited_once_with(function, original)
    assert arguments == original
