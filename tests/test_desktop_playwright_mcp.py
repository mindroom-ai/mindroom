"""Tests for the local Playwright MCP extension provider."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from mindroom.desktop.playwright_mcp import (
    _MAX_RESULT_JSON_BYTES,
    PLAYWRIGHT_MCP_PACKAGE,
    PlaywrightActionOutcomeUnknownError,
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
    navigate = _mcp_calls("navigate", {"targetId": "2", "targetUrl": "https://example.com/form"})
    assert [(call.tool_name, call.arguments) for call in navigate] == [
        ("browser_tabs", {"action": "select", "index": 2}),
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
async def test_provider_executes_multi_step_tab_selection_before_navigation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One Matrix command may select a tab and then navigate it in-order locally."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    call_tool = AsyncMock(side_effect=[_text_result("selected"), _text_result("navigated")])
    monkeypatch.setattr(provider, "_call_tool", call_tool)

    result = await provider.execute(
        "navigate",
        {"targetId": "1", "targetUrl": "https://example.com/checkout"},
    )

    assert result.payload["result"] == "navigated"
    assert [call.args for call in call_tool.await_args_list] == [
        ("browser_tabs", {"action": "select", "index": 1}),
        ("browser_navigate", {"url": "https://example.com/checkout"}),
    ]


@pytest.mark.asyncio
async def test_failed_tab_selection_never_mutates_the_previous_tab(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An MCP error from the selection prefix aborts before the navigation call."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    call_tool = AsyncMock(return_value=_text_result("tab vanished", error=True))
    monkeypatch.setattr(provider, "_call_tool", call_tool)

    with pytest.raises(PlaywrightActionOutcomeUnknownError, match="tab vanished"):
        await provider.execute(
            "navigate",
            {"targetId": "1", "targetUrl": "https://example.com/checkout"},
        )

    call_tool.assert_awaited_once_with("browser_tabs", {"action": "select", "index": 1})


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
