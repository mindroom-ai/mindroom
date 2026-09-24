"""Browser uploads retain authorized bytes across filesystem races."""

from __future__ import annotations

import asyncio
import shutil
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import async_playwright

from mindroom.attachments import register_local_attachment
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.custom_tools.browser import BrowserTools, _BrowserProfileState, _BrowserTabState
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO

    from mindroom.config.models import FileAccess
    from mindroom.file_access import AuthorizedFile
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _upload_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace_root: Path | None = None,
    file_access: FileAccess = "workspace",
) -> tuple[BrowserTools, AsyncMock, Path]:
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    root = runtime_paths.storage_root / "browser"
    root.mkdir(parents=True)
    tool = BrowserTools(runtime_paths, tool_output_workspace_root=workspace_root, file_access=file_access)
    consumer = AsyncMock()
    page: Any = SimpleNamespace(
        locator=MagicMock(return_value=SimpleNamespace(first=SimpleNamespace(set_input_files=consumer))),
        is_closed=lambda: False,
        close=AsyncMock(),
    )
    tab = _BrowserTabState(target_id="tab-1", page=page, refs={"e1": "input[type=file]"})
    state = _BrowserProfileState(
        playwright=SimpleNamespace(stop=AsyncMock()),
        context=SimpleNamespace(close=AsyncMock()),
        tabs={"tab-1": tab},
    )
    tool._profiles["mindroom"] = state
    monkeypatch.setattr(tool, "_ensure_profile", AsyncMock(return_value=state))
    monkeypatch.setattr(tool, "_resolve_tab", AsyncMock(return_value=("tab-1", tab)))
    return tool, consumer, root


async def _upload(tool: BrowserTools, paths: list[Path | str]) -> dict[str, Any]:
    return await tool._upload(
        profile_name="mindroom",
        target_id=None,
        paths=[str(path) for path in paths],
        ref="e1",
        input_ref=None,
        element=None,
        timeout_ms=1234,
    )


def _swap_fixture(root: Path, part: str) -> tuple[Path, Callable[[], None]]:
    parent = root / "nested"
    parent.mkdir()
    source = parent / "upload.txt"
    source.write_bytes(b"authorized bytes")
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "upload.txt").write_bytes(b"private bytes")
    (outside / "nested").mkdir()
    (outside / "nested" / "upload.txt").write_bytes(b"private bytes")

    def swap() -> None:
        if part == "leaf":
            source.unlink()
            source.symlink_to(outside / "upload.txt")
        elif part == "root":
            root.rename(root.with_name("old-browser"))
            root.symlink_to(outside, target_is_directory=True)
        else:
            parent.rename(root / "old-nested")
            parent.symlink_to(outside, target_is_directory=True)

    return source, swap


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["root", "parent", "leaf"])
async def test_upload_consumes_original_bytes_when_source_swaps_during_browser_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    part: str,
) -> None:
    """A browser pathname reopen cannot follow a replaced source entry."""
    tool, consumer, root = _upload_tool(tmp_path, monkeypatch)
    source, swap = _swap_fixture(root, part)
    consumed: list[bytes] = []
    staging: list[Path] = []

    async def consume(paths: list[str], *, timeout: int) -> None:  # noqa: ASYNC109
        assert timeout == 1234
        swap()
        staging.extend(Path(path) for path in paths)
        consumed.extend(path.read_bytes() for path in staging)

    consumer.side_effect = consume
    result = await _upload(tool, [source])

    assert consumed == [b"authorized bytes"]
    assert result["paths"] == [str(source)]
    assert staging[0].name == "upload.txt"
    assert staging[0].read_bytes() == b"authorized bytes"
    await tool._close_tab("mindroom", "tab-1")
    assert not staging[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["root", "parent", "leaf"])
async def test_upload_rejects_source_swapped_after_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    part: str,
) -> None:
    """The confined open rejects links introduced after the path policy check."""
    tool, consumer, root = _upload_tool(tmp_path, monkeypatch)
    source, swap = _swap_fixture(root, part)
    resolve = tool._resolve_upload_path

    def resolve_then_swap(path: str) -> AuthorizedFile:
        resolved = resolve(path)
        swap()
        return resolved

    monkeypatch.setattr(tool, "_resolve_upload_path", resolve_then_swap)
    with pytest.raises((OSError, ValueError)):
        await _upload(tool, [source])
    consumer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", ["default", "explicit", "worker"])
async def test_upload_rejects_replaced_authorized_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configuration: str,
) -> None:
    """A replaced artifact root cannot authorize its new outside destination."""
    tool, consumer, root = _upload_tool(tmp_path, monkeypatch)
    if configuration != "default":
        tool._configured_output_dir = root
    if configuration == "worker":
        tool._worker_workspace = root.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "upload.txt").write_bytes(b"private bytes")
    root.rmdir()
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        await _upload(tool, [root / "upload.txt"])
    consumer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["primary", "worker"])
@pytest.mark.parametrize("requested", ["relative", "absolute"])
async def test_upload_rejects_replaced_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding: str,
    requested: str,
) -> None:
    """A bound workspace root swapped for a link cannot authorize its new outside destination."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool, consumer, _root = _upload_tool(tmp_path, monkeypatch, workspace_root=workspace)
    if binding == "worker":
        tool._worker_workspace = workspace.resolve()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "upload.txt").write_bytes(b"private bytes")
    workspace.rmdir()
    workspace.symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        await _upload(tool, ["upload.txt" if requested == "relative" else workspace / "upload.txt"])
    consumer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["relative", "absolute"])
async def test_upload_rejects_workspace_root_replaced_before_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
) -> None:
    """A primary workspace root that is already a link when the tool is built must not authorize its target."""
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "upload.txt").write_bytes(b"private bytes")
    workspace.symlink_to(outside, target_is_directory=True)
    tool, consumer, _root = _upload_tool(tmp_path, monkeypatch, workspace_root=workspace)

    with pytest.raises((OSError, ValueError)):
        await _upload(tool, ["upload.txt" if requested == "relative" else workspace / "upload.txt"])
    consumer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_upload_removes_staging_after_browser_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    """Failure and cancellation remove every private staged upload."""
    tool, consumer, root = _upload_tool(tmp_path, monkeypatch)
    source = root / "upload.txt"
    source.write_bytes(b"authorized bytes")
    staging: list[Path] = []

    async def fail(paths: list[str], *, timeout: int) -> None:  # noqa: ASYNC109
        assert timeout == 1234
        staging.extend(Path(path) for path in paths)
        assert staging[0].read_bytes() == b"authorized bytes"
        raise failure

    consumer.side_effect = fail
    with pytest.raises(failure):
        await _upload(tool, [source])
    assert staging
    assert not staging[0].exists()
    assert source.read_bytes() == b"authorized bytes"


@pytest.mark.asyncio
async def test_upload_preserves_internal_links_duplicate_names_and_private_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal aliases retain canonical filenames and duplicate basenames keep distinct bytes."""
    tool, consumer, root = _upload_tool(tmp_path, monkeypatch)
    first = root / "first" / "report.txt"
    second = root / "second" / "report.txt"
    for source, content in ((first, b"first report"), (second, b"second report")):
        source.parent.mkdir()
        source.write_bytes(content)
    alias = root / "alias.txt"
    alias.symlink_to(first)
    consumed: list[tuple[str, bytes]] = []

    async def consume(paths: list[str], *, timeout: int) -> None:  # noqa: ASYNC109
        assert timeout == 1234
        assert len(set(paths)) == 2
        for path in map(Path, paths):
            assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
            consumed.append((path.name, path.read_bytes()))

    consumer.side_effect = consume
    result = await _upload(tool, [alias, second])

    assert consumed == [("report.txt", b"first report"), ("report.txt", b"second report")]
    assert result["paths"] == [str(first), str(second)]
    await tool.aclose()


@pytest.mark.asyncio
async def test_upload_keeps_large_files_as_file_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files above Playwright's buffer limit use disk paths without a new upload cap."""
    tool, consumer, root = _upload_tool(tmp_path, monkeypatch)
    source = root / "large.bin"
    size = 51 * 1024 * 1024
    with source.open("wb") as output:
        output.write(b"first")
        output.seek(size - 4)
        output.write(b"last")
    consumed: list[int] = []

    async def consume(paths: list[str], *, timeout: int) -> None:  # noqa: ASYNC109
        assert timeout == 1234
        [path] = map(Path, paths)
        consumed.append(path.stat().st_size)
        with path.open("rb") as uploaded:
            assert uploaded.read(5) == b"first"
            uploaded.seek(size - 4)
            assert uploaded.read(4) == b"last"

    consumer.side_effect = consume
    result = await _upload(tool, [source])

    assert consumed == [size]
    assert result["paths"] == [str(source)]
    await tool.aclose()


def _upload_context(
    tool: BrowserTools,
    storage: Path,
    attachment_ids: tuple[str, ...] = (),
) -> ToolRuntimeContext:
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id=None, reply_to_event_id=None),
        requester_id="@alice:example.org",
        client=MagicMock(),
        config=MagicMock(),
        runtime_paths=tool._runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        storage_path=storage,
        attachment_ids=attachment_ids,
    )


def _primary_upload_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_access: FileAccess = "workspace",
) -> tuple[BrowserTools, AsyncMock, Path, Path]:
    storage = tmp_path / "storage"
    workspace = agent_workspace_root_path(storage, "general")
    workspace.mkdir(parents=True)
    tool, consumer, _root = _upload_tool(tmp_path, monkeypatch, workspace_root=workspace, file_access=file_access)
    return tool, consumer, storage, workspace


def _capture_uploads(consumer: AsyncMock) -> list[bytes]:
    consumed: list[bytes] = []

    async def consume(paths: list[str], *, timeout: int) -> None:  # noqa: ASYNC109
        assert timeout == 1234
        consumed.extend(Path(path).read_bytes() for path in paths)

    consumer.side_effect = consume
    return consumed


@pytest.mark.asyncio
async def test_primary_upload_reads_agent_workspace_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A primary-process browser uploads the agent's workspace files by absolute or relative path."""
    tool, consumer, storage, workspace = _primary_upload_tool(tmp_path, monkeypatch)
    report = workspace / "reports" / "report.txt"
    report.parent.mkdir()
    report.write_bytes(b"workspace report")
    (workspace / "att_named.txt").write_bytes(b"workspace att name")
    consumed = _capture_uploads(consumer)

    with tool_runtime_context(_upload_context(tool, storage)):
        result = await _upload(tool, [report, "reports/report.txt", "./att_named.txt"])

    assert consumed == [b"workspace report", b"workspace report", b"workspace att name"]
    assert result["paths"] == [str(report), str(report), str(workspace / "att_named.txt")]
    await tool.aclose()


@pytest.mark.asyncio
async def test_primary_upload_reads_received_attachment_by_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attachments available in the conversation upload through their att_* IDs."""
    tool, consumer, storage, _workspace = _primary_upload_tool(tmp_path, monkeypatch)
    media = storage / "incoming_media" / "photo.png"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"received photo")
    record = register_local_attachment(storage, media, kind="image", attachment_id="att_photo")
    assert record is not None
    consumed = _capture_uploads(consumer)

    with tool_runtime_context(_upload_context(tool, storage, attachment_ids=("att_photo",))):
        result = await _upload(tool, ["att_photo"])

    assert consumed == [b"received photo"]
    assert result["paths"] == [str(record.local_path.resolve())]
    await tool.aclose()


@pytest.mark.asyncio
async def test_primary_upload_rejects_attachments_outside_context_and_replaced_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attachment IDs require context availability, and a replaced recorded file is never followed."""
    tool, consumer, storage, _workspace = _primary_upload_tool(tmp_path, monkeypatch)
    media = storage / "incoming_media" / "photo.png"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"received photo")
    record = register_local_attachment(storage, media, kind="image", attachment_id="att_photo")
    assert record is not None
    secret = storage / "credentials" / "secret_credentials.json"
    secret.parent.mkdir()
    secret.write_bytes(b"secret")

    with tool_runtime_context(_upload_context(tool, storage)), pytest.raises(ValueError, match="not available"):
        await _upload(tool, ["att_photo"])
    record.local_path.unlink()
    record.local_path.symlink_to(secret)
    with (
        tool_runtime_context(_upload_context(tool, storage, attachment_ids=("att_photo",))),
        pytest.raises((OSError, ValueError)),
    ):
        await _upload(tool, ["att_photo"])
    consumer.assert_not_awaited()
    await tool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative_path",
    [
        "credentials/secret_credentials.json",
        "encryption_keys/bot.db",
        "matrix_state.yaml",
        "attachments/att_photo.json",
        "incoming_media/photo.png",
        "agents/other/workspace/notes.txt",
    ],
)
async def test_primary_upload_rejects_runtime_state_and_other_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    """Runtime storage outside the artifact directory and this agent's workspace stays unreadable by path."""
    tool, consumer, storage, _workspace = _primary_upload_tool(tmp_path, monkeypatch)
    source = storage / relative_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"private bytes")

    with (
        tool_runtime_context(_upload_context(tool, storage)),
        pytest.raises(ValueError, match="inside the agent workspace"),
    ):
        await _upload(tool, [source])
    consumer.assert_not_awaited()
    await tool.aclose()


@pytest.mark.asyncio
async def test_primary_upload_unrestricted_reads_any_readable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrestricted file access uploads runtime storage files a workspace agent cannot reach."""
    tool, consumer, storage, _workspace = _primary_upload_tool(tmp_path, monkeypatch, "unrestricted")
    source = storage / "credentials" / "x.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"trusted setup")
    consumed = _capture_uploads(consumer)

    with tool_runtime_context(_upload_context(tool, storage)):
        result = await _upload(tool, [source])

    assert consumed == [b"trusted setup"]
    assert result["paths"] == [str(source)]
    await tool.aclose()


@pytest.mark.asyncio
async def test_worker_upload_unrestricted_reads_any_worker_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrestricted worker-bound browser uploads files outside its worker workspace."""
    tool, consumer, root = _upload_tool(tmp_path, monkeypatch, file_access="unrestricted")
    tool._worker_workspace = root.parent
    tool._configured_output_dir = root
    outside_file = tmp_path / "outside" / "notes.txt"
    outside_file.parent.mkdir()
    outside_file.write_bytes(b"outside notes")
    consumed = _capture_uploads(consumer)

    result = await _upload(tool, [outside_file])

    assert consumed == [b"outside notes"]
    assert result["paths"] == [str(outside_file)]
    await tool.aclose()


@pytest.mark.asyncio
async def test_worker_upload_reads_only_worker_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker-bound browser reads its worker workspace and treats att_* as an ordinary path."""
    tool, consumer, root = _upload_tool(tmp_path, monkeypatch, workspace_root=tmp_path / "primary-workspace")
    tool._worker_workspace = root.parent
    tool._configured_output_dir = root
    workspace_file = root.parent / "notes.txt"
    workspace_file.write_bytes(b"worker notes")
    primary_file = tmp_path / "primary-workspace" / "notes.txt"
    primary_file.parent.mkdir()
    primary_file.write_bytes(b"primary notes")
    consumed = _capture_uploads(consumer)

    result = await _upload(tool, [workspace_file])
    with pytest.raises(ValueError, match="inside the agent workspace"):
        await _upload(tool, [primary_file])
    with pytest.raises(ValueError, match="must be an existing file"):
        await _upload(tool, ["att_photo"])

    assert consumed == [b"worker notes"]
    assert result["paths"] == [str(workspace_file)]
    await tool.aclose()


@pytest.mark.asyncio
async def test_upload_cancellation_drains_snapshot_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation waits for a running bounded copy, then removes its private files."""
    tool, consumer, root = _upload_tool(tmp_path, monkeypatch)
    source = root / "upload.txt"
    source.write_bytes(b"authorized bytes")
    copy_started = asyncio.Event()
    release_copy = threading.Event()
    loop = asyncio.get_running_loop()
    original_copy = shutil.copyfileobj
    staging: list[Path] = []

    def blocked_copy(source: BinaryIO, output: BinaryIO, length: int) -> None:
        assert 0 < length <= 1024 * 1024
        staging.append(Path(output.name))
        loop.call_soon_threadsafe(copy_started.set)
        assert release_copy.wait(timeout=5)
        original_copy(source, output, length)

    monkeypatch.setattr(shutil, "copyfileobj", blocked_copy)
    task = asyncio.create_task(_upload(tool, [source]))
    try:
        await asyncio.wait_for(copy_started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert staging[0].exists()
    finally:
        release_copy.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not staging[0].exists()
    consumer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_upload_profile_teardown_cleans_snapshots_when_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException] | None,
) -> None:
    """Profile teardown owns staged uploads even when no page-close event arrives."""
    tool, consumer, root = _upload_tool(tmp_path, monkeypatch)
    source = root / "upload.txt"
    source.write_bytes(b"authorized bytes")
    staging: list[Path] = []

    async def consume(paths: list[str], *, timeout: int) -> None:  # noqa: ASYNC109
        assert timeout == 1234
        staging.extend(Path(path) for path in paths)

    consumer.side_effect = consume
    await _upload(tool, [source])
    assert staging[0].read_bytes() == b"authorized bytes"
    tool._profiles["mindroom"].context.close.side_effect = failure

    if failure is None:
        await tool._stop_profile("mindroom")
    else:
        with pytest.raises(failure):
            await tool._stop_profile("mindroom")

    assert not staging[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [20, 51 * 1024 * 1024])
async def test_upload_bytes_survive_tool_return_in_real_browser(tmp_path: Path, size: int) -> None:
    """Chromium reads both small and large selected files after the upload tool returns."""
    executable = shutil.which("chromium")
    if executable is None:
        pytest.skip("Chromium required for upload integration")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    root = runtime_paths.storage_root / "browser"
    root.mkdir(parents=True)
    source = root / "upload.txt"
    with source.open("wb") as output:
        output.write(b"first")
        output.seek(size - 4)
        output.write(b"last")
    tool = BrowserTools(runtime_paths)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content('<input type="file">')
        state = _BrowserProfileState(playwright=playwright, context=context)
        tool._profiles["mindroom"] = state
        target_id = tool._register_tab(state, page)
        state.tabs[target_id].refs["e1"] = "input[type=file]"
        state.active_target_id = target_id
        try:
            result = await _upload(tool, [source])
            source.unlink()
            selected = await page.locator("input").evaluate(
                """async (input) => {
                    const file = input.files[0];
                    return {name: file.name, size: file.size,
                            head: await file.slice(0, 5).text(), tail: await file.slice(-4).text()};
                }""",
            )
            assert selected == {"name": "upload.txt", "size": size, "head": "first", "tail": "last"}
            assert result["paths"] == [str(source)]
            snapshots = [Path(staging.name) for staging in state.tabs[target_id].upload_staging]
            assert snapshots
            await tool._close_tab("mindroom", target_id)
            assert all(not snapshot.exists() for snapshot in snapshots)
        finally:
            await browser.close()
