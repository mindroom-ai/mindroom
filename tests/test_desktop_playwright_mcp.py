"""Tests for the local Playwright MCP extension provider."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from mindroom.desktop.playwright_mcp import (
    _MAX_RESULT_JSON_BYTES,
    PLAYWRIGHT_MCP_PACKAGE,
    PlaywrightBrowserError,
    PlaywrightMCPBrowserProvider,
    _act_call,
    _mcp_calls,
    _provider_result,
    _QueuedCall,
    browser_action_requires_control,
)

if TYPE_CHECKING:
    from pathlib import Path

_TEST_EXTENSION_TOKEN = "test-extension-token"  # noqa: S105 - Test-only provider credential.


class _FailingStdioContext:
    async def __aenter__(self) -> None:
        message = "extension startup failed"
        raise RuntimeError(message)

    async def __aexit__(self, *_args: object) -> None:
        return None


def _text_result(text: str = "ok", *, error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=error)


def test_browser_action_policy_keeps_observation_available_without_control() -> None:
    """Snapshots stay observe-only while navigation and form actions require the lease."""
    assert browser_action_requires_control("tabs") is False
    assert browser_action_requires_control("snapshot") is False
    assert browser_action_requires_control("snapshot", {"targetId": "2"}) is True
    assert browser_action_requires_control("screenshot") is False
    assert browser_action_requires_control("navigate") is True
    assert browser_action_requires_control("act") is True


def test_provider_launches_pinned_extension_server_for_existing_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The local process uses extension mode, an explicit browser, and the real user-data root."""
    executable = tmp_path / "Brave Browser"
    user_data_dir = tmp_path / "Brave-Browser"
    provider = PlaywrightMCPBrowserProvider(
        output_dir=tmp_path / "output",
        executable_path=executable,
        user_data_dir=user_data_dir,
        extension_token=_TEST_EXTENSION_TOKEN,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-browser-child")

    assert provider._server_args() == [
        "--yes",
        PLAYWRIGHT_MCP_PACKAGE,
        "--extension",
        "--caps",
        "vision,pdf",
        "--output-dir",
        str((tmp_path / "output").resolve()),
        "--output-mode",
        "stdout",
        "--executable-path",
        str(executable.resolve()),
    ]
    environment = provider._server_environment()
    assert environment["PWTEST_EXTENSION_USER_DATA_DIR"] == str(user_data_dir.resolve())
    assert environment["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] == _TEST_EXTENSION_TOKEN
    assert "OPENAI_API_KEY" not in environment


def test_browser_actions_map_to_high_level_playwright_mcp_tools() -> None:
    """Tab, navigation, snapshot, and screenshot actions avoid raw CDP calls."""
    assert _mcp_calls("tabs", {})[0].tool_name == "browser_tabs"
    assert _mcp_calls("open", {"targetUrl": "https://example.com"})[0].arguments == {
        "action": "new",
        "url": "https://example.com",
    }
    navigate = _mcp_calls("navigate", {"targetUrl": "https://example.com/form"})
    assert [(call.tool_name, call.arguments) for call in navigate] == [
        ("browser_navigate", {"url": "https://example.com/form"}),
    ]
    snapshot = _mcp_calls("snapshot", {"selector": "main", "depth": 8, "maxChars": 4000})
    assert snapshot[-1].tool_name == "browser_snapshot"
    assert snapshot[-1].arguments == {"target": "main", "depth": 8}
    screenshot = _mcp_calls("screenshot", {"ref": "e7", "type": "jpeg", "fullPage": False})
    assert screenshot[-1].tool_name == "browser_take_screenshot"
    assert screenshot[-1].arguments == {
        "element": "e7",
        "target": "e7",
        "type": "jpeg",
        "scale": "css",
        "fullPage": False,
    }


def test_act_mapping_covers_semantic_interaction_parity() -> None:
    """The stable browser act vocabulary maps to Playwright's semantic primitives."""
    click = _act_call({"kind": "click", "ref": "e3", "doubleClick": True})
    assert click.tool_name == "browser_click"
    assert click.arguments == {"element": "e3", "target": "e3", "doubleClick": True}

    fill = _act_call(
        {
            "kind": "fill",
            "fields": [
                {"ref": "e4", "name": "Full name", "type": "textbox", "value": "Ada Lovelace"},
                {"ref": "e5", "name": "Updates", "type": "checkbox", "value": "true"},
            ],
        },
    )
    assert fill.tool_name == "browser_fill_form"
    assert fill.arguments["fields"] == [
        {"element": "Full name", "name": "Full name", "target": "e4", "type": "textbox", "value": "Ada Lovelace"},
        {"element": "Updates", "name": "Updates", "target": "e5", "type": "checkbox", "value": "true"},
    ]

    evaluate = _act_call({"kind": "evaluate", "fn": "element => element.textContent", "ref": "e6"})
    assert evaluate.tool_name == "browser_evaluate"
    assert evaluate.arguments == {
        "function": "element => element.textContent",
        "element": "e6",
        "target": "e6",
    }


def test_provider_result_preserves_model_text_and_image() -> None:
    """Screenshots become bounded Matrix media while accessibility text stays structured."""
    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    result = CallToolResult(
        content=[
            TextContent(type="text", text="Page snapshot"),
            ImageContent(type="image", data=base64.b64encode(image_bytes).decode(), mimeType="image/png"),
        ],
        isError=False,
    )

    provider_result = _provider_result("screenshot", result, max_chars=100)

    assert provider_result.payload["result"] == "Page snapshot"
    assert provider_result.image is not None
    assert provider_result.image.content == image_bytes
    assert provider_result.image.mime_type == "image/png"


def test_provider_result_respects_encrypted_matrix_json_budget() -> None:
    """Multibyte page text remains bounded after nio's ASCII-escaped JSON encoding."""
    provider_result = _provider_result("snapshot", _text_result("漢" * 32_000), max_chars=32_000)
    text = provider_result.payload["result"]

    assert isinstance(text, str)
    assert len(json.dumps(text, separators=(",", ":")).encode()) <= _MAX_RESULT_JSON_BYTES
    assert text.endswith("\n…")


def test_provider_result_rejects_mcp_tool_errors() -> None:
    """An MCP error is not mislabeled as a successful browser action."""
    with pytest.raises(PlaywrightBrowserError, match="extension disconnected"):
        _provider_result("tabs", _text_result("extension disconnected", error=True), max_chars=100)


@pytest.mark.asyncio
async def test_provider_navigates_only_the_current_extension_tab(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One Matrix command navigates without trusting a mutable tab-list index."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    call_tool = AsyncMock(return_value=_text_result("navigated"))
    monkeypatch.setattr(provider, "_call_tool", call_tool)

    result = await provider.execute(
        "navigate",
        {"targetUrl": "https://example.com/checkout"},
    )

    assert result.payload["result"] == "navigated"
    call_tool.assert_awaited_once_with("browser_navigate", {"url": "https://example.com/checkout"})


@pytest.mark.asyncio
async def test_provider_removes_only_its_transient_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Screenshot bytes survive while the exact MCP scratch file is removed."""
    image_bytes = b"\x89PNG\r\n\x1a\nlive screenshot"
    unrelated = tmp_path / "page-keep.png"
    unrelated.write_bytes(b"keep")
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    monkeypatch.setattr(PlaywrightMCPBrowserProvider, "running", property(lambda _self: True))

    async def take_screenshot(tool_name: str, arguments: dict[str, object]) -> CallToolResult:
        assert tool_name == "browser_take_screenshot"
        filename = arguments["filename"]
        assert isinstance(filename, str)
        screenshot_path = tmp_path / filename
        screenshot_path.write_bytes(image_bytes)
        return _text_result(f"Screenshot saved as {filename}")

    call_tool = AsyncMock(side_effect=take_screenshot)
    monkeypatch.setattr(provider, "_call_tool", call_tool)

    result = await provider.execute("screenshot", {})

    assert result.image is not None
    assert result.image.content == image_bytes
    assert result.image.mime_type == "image/png"
    assert unrelated.read_bytes() == b"keep"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["page-keep.png"]


@pytest.mark.asyncio
async def test_provider_removes_transient_screenshot_when_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An invalid MCP image cannot strand its generated plaintext file."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    monkeypatch.setattr(PlaywrightMCPBrowserProvider, "running", property(lambda _self: True))

    async def take_empty_screenshot(_tool_name: str, arguments: dict[str, object]) -> CallToolResult:
        filename = arguments["filename"]
        assert isinstance(filename, str)
        (tmp_path / filename).write_bytes(b"")
        return _text_result("Screenshot completed")

    monkeypatch.setattr(provider, "_call_tool", AsyncMock(side_effect=take_empty_screenshot))

    with pytest.raises(PlaywrightBrowserError, match="must contain between"):
        await provider.execute("screenshot", {})

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_observation_cannot_start_the_extension_without_control(tmp_path: Path) -> None:
    """An observe-only command cannot launch or foreground the user's browser."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)

    with pytest.raises(PlaywrightBrowserError, match=r"browser\(action='start'"):
        await provider.execute("tabs", {})

    assert provider.running is False


@pytest.mark.skipif(os.name == "nt", reason="Unix permission bits are not authoritative on Windows")
@pytest.mark.asyncio
async def test_actor_hardens_existing_browser_workspace_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A permissive pre-existing scratch directory becomes owner-only before MCP starts."""
    output_dir = tmp_path / "desktop-browser"
    output_dir.mkdir(mode=0o777)
    output_dir.chmod(0o777)
    provider = PlaywrightMCPBrowserProvider(output_dir=output_dir)
    monkeypatch.setattr("mindroom.desktop.playwright_mcp.shutil.which", lambda _command: "/usr/bin/npx")
    monkeypatch.setattr(provider, "_run_actor", AsyncMock())
    future: asyncio.Future[CallToolResult] = asyncio.get_running_loop().create_future()

    provider._start_actor(_QueuedCall("browser_tabs", {"action": "list"}, future))
    assert provider._actor_task is not None
    await provider._actor_task

    assert output_dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_timed_out_screenshot_is_removed_after_late_mcp_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A screenshot written after the caller times out is removed by the actor that wrote it."""
    screenshot_finished = asyncio.Event()

    class FakeStdio:
        async def __aenter__(self) -> tuple[object, object]:
            return object(), object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def call_tool(
            self,
            tool_name: str,
            arguments: dict[str, object],
            **_kwargs: object,
        ) -> CallToolResult:
            if tool_name == "browser_tabs":
                return _text_result("started")
            await asyncio.sleep(0.05)
            filename = arguments["filename"]
            assert isinstance(filename, str)
            (tmp_path / filename).write_bytes(b"late screenshot")
            screenshot_finished.set()
            return _text_result("captured")

    monkeypatch.setattr("mindroom.desktop.playwright_mcp.shutil.which", lambda _command: "/usr/bin/npx")
    monkeypatch.setattr("mindroom.desktop.playwright_mcp.stdio_client", lambda _parameters: FakeStdio())
    monkeypatch.setattr("mindroom.desktop.playwright_mcp.ClientSession", FakeSession)
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    provider._call_timeout_seconds = 0.01
    await provider.execute("start", {})

    with pytest.raises(PlaywrightBrowserError, match="did not answer"):
        await provider.execute("screenshot", {})

    await asyncio.wait_for(screenshot_finished.wait(), timeout=1)
    await asyncio.sleep(0)
    assert list(tmp_path.iterdir()) == []
    await provider.close()


@pytest.mark.asyncio
async def test_oversized_screenshot_uses_a_bounded_file_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A huge MCP scratch file is rejected without calling the unbounded read_bytes helper."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    monkeypatch.setattr(PlaywrightMCPBrowserProvider, "running", property(lambda _self: True))

    async def take_oversized_screenshot(_tool_name: str, arguments: dict[str, object]) -> CallToolResult:
        filename = arguments["filename"]
        assert isinstance(filename, str)
        with (tmp_path / filename).open("wb") as image_file:
            image_file.truncate(10 * 1024 * 1024 + 1)
        return _text_result("captured")

    def reject_unbounded_read(_path: Path) -> bytes:
        message = "Path.read_bytes must not be used for MCP screenshots"
        raise AssertionError(message)

    monkeypatch.setattr(provider, "_call_tool", AsyncMock(side_effect=take_oversized_screenshot))
    monkeypatch.setattr(type(tmp_path), "read_bytes", reject_unbounded_read)

    with pytest.raises(PlaywrightBrowserError, match="must contain between"):
        await provider.execute("screenshot", {})

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("action", ["focus", "snapshot", "navigate", "close"])
def test_mutable_playwright_tab_indices_are_rejected(action: str) -> None:
    """The desktop extension never treats a mutable tab-list index as a stable target."""
    parameters: dict[str, object] = {"targetId": "1"}
    if action == "navigate":
        parameters["targetUrl"] = "https://example.com/checkout"

    with pytest.raises(PlaywrightBrowserError, match="tab indices can change"):
        _mcp_calls(action, parameters)


@pytest.mark.asyncio
async def test_uploads_are_confined_to_the_browser_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The extension child never receives an upload path outside its documented workspace."""
    output_dir = tmp_path / "browser"
    output_dir.mkdir()
    allowed_file = output_dir / "invoice.txt"
    allowed_file.write_text("invoice")
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("secret")
    provider = PlaywrightMCPBrowserProvider(output_dir=output_dir)
    call_tool = AsyncMock(return_value=_text_result("uploaded"))
    monkeypatch.setattr(provider, "_call_tool", call_tool)

    await provider.execute("upload", {"paths": ["invoice.txt"]})

    call_tool.assert_awaited_once_with("browser_file_upload", {"paths": [str(allowed_file.resolve())]})
    with pytest.raises(PlaywrightBrowserError, match="must exist under"):
        await provider.execute("upload", {"paths": [str(outside_file)]})


@pytest.mark.asyncio
async def test_actor_skips_a_call_whose_request_already_timed_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A queued mutation cannot execute after its Matrix-side caller has abandoned it."""
    call_tool = AsyncMock(return_value=_text_result("late mutation"))

    class FakeStdio:
        async def __aenter__(self) -> tuple[object, object]:
            return object(), object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeSession:
        def __init__(self, *_args: object) -> None:
            pass

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def call_tool(self, *args: object, **kwargs: object) -> CallToolResult:
            return await call_tool(*args, **kwargs)

    monkeypatch.setattr("mindroom.desktop.playwright_mcp.stdio_client", lambda _parameters: FakeStdio())
    monkeypatch.setattr("mindroom.desktop.playwright_mcp.ClientSession", FakeSession)
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    queue: asyncio.Queue[_QueuedCall | None] = asyncio.Queue()
    future: asyncio.Future[CallToolResult] = asyncio.get_running_loop().create_future()
    future.cancel()
    queue.put_nowait(_QueuedCall("browser_click", {"target": "e1"}, future))
    queue.put_nowait(None)

    await provider._run_actor(queue)

    call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_is_lazy_until_extension_use(tmp_path: Path) -> None:
    """Enabling the capability does not launch or take over a browser before first use."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)

    result = await provider.execute("status", {})

    assert result.payload == {
        "action": "status",
        "provider": "playwright_mcp_extension",
        "running": False,
        "status": "ok",
    }
    assert provider.running is False


@pytest.mark.asyncio
async def test_mcp_startup_failure_reaches_first_queued_call_immediately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A child-process startup failure must not strand the first request until its timeout."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path, call_timeout_seconds=5)
    monkeypatch.setattr(
        "mindroom.desktop.playwright_mcp.stdio_client",
        lambda _parameters: _FailingStdioContext(),
    )

    with pytest.raises(PlaywrightBrowserError, match="extension startup failed"):
        await asyncio.wait_for(provider._call_tool("browser_tabs", {"action": "list"}), timeout=0.5)

    assert provider.running is False


@pytest.mark.asyncio
async def test_permanent_close_rejects_calls_while_actor_finishes(tmp_path: Path) -> None:
    """A final close cannot race with a fresh MCP actor start."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    queue: asyncio.Queue[_QueuedCall | None] = asyncio.Queue()
    sentinel_seen = asyncio.Event()
    finish_actor = asyncio.Event()

    async def actor() -> None:
        assert await queue.get() is None
        sentinel_seen.set()
        await finish_actor.wait()

    provider._queue = queue
    provider._actor_task = asyncio.create_task(actor())
    close_task = asyncio.create_task(provider.close())
    await sentinel_seen.wait()

    with pytest.raises(PlaywrightBrowserError, match="provider is closed"):
        await provider._call_tool("browser_tabs", {"action": "list"})

    finish_actor.set()
    await close_task
    assert provider.running is False


@pytest.mark.asyncio
async def test_browser_stop_remains_restartable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The user-facing stop action releases MCP without permanently closing the provider."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)

    def complete_start(queued_call: _QueuedCall) -> None:
        queued_call.future.set_result(_text_result("started"))

    monkeypatch.setattr(provider, "_start_actor", complete_start)

    stopped = await provider.execute("stop", {})
    started = await provider.execute("start", {})

    assert stopped.payload["running"] is False
    assert started.payload["result"] == "started"
