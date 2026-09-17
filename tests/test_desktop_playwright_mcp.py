"""Tests for the local Playwright MCP extension provider."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import anyio
import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from mindroom.desktop.playwright_mcp import (
    _MAX_RESULT_JSON_BYTES,
    PLAYWRIGHT_MCP_PACKAGE,
    PlaywrightActionOutcomeUnknownError,
    PlaywrightBrowserError,
    PlaywrightMCPBrowserProvider,
    _mcp_calls,
    _provider_result,
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
        "vision",
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
async def test_provider_opens_and_navigates_a_new_tab_in_one_upstream_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The upstream call creates a new Tab and navigates that retained object."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    call_tool = AsyncMock(return_value=_text_result("opened"))
    monkeypatch.setattr(provider, "_call_tool", call_tool)

    result = await provider.execute(
        "open",
        {"targetUrl": "https://example.com/checkout"},
    )

    assert result.payload["result"] == "opened"
    assert result.payload["stable_targeting"] is False
    call_tool.assert_awaited_once_with("browser_tabs", {"action": "new", "url": "https://example.com/checkout"})


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
    session = provider._new_session()
    await session.close()

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
            writer, reader = anyio.create_memory_object_stream(0)
            return reader, writer

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
            # Simulate output arriving during cancellation cleanup.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0.05)
            filename = arguments["filename"]
            assert isinstance(filename, str)
            (tmp_path / filename).write_bytes(b"late screenshot")
            screenshot_finished.set()
            return _text_result("captured")

    monkeypatch.setattr("mindroom.desktop.playwright_mcp.shutil.which", lambda _command: "/usr/bin/npx")
    monkeypatch.setattr("mindroom.playwright_mcp_session.stdio_client", lambda _parameters: FakeStdio())
    monkeypatch.setattr("mindroom.playwright_mcp_session.ClientSession", FakeSession)
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


@pytest.mark.asyncio
async def test_failed_control_action_reports_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed control call may have taken effect before the extension disconnected."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    call_tool = AsyncMock(return_value=_text_result("extension disconnected", error=True))
    monkeypatch.setattr(provider, "_call_tool", call_tool)

    with pytest.raises(PlaywrightActionOutcomeUnknownError, match="extension disconnected"):
        await provider.execute("open", {"targetUrl": "https://example.com/checkout"})

    call_tool.assert_awaited_once_with("browser_tabs", {"action": "new", "url": "https://example.com/checkout"})


@pytest.mark.parametrize("action", ["focus", "snapshot", "navigate", "close"])
def test_mutable_playwright_tab_indices_are_rejected(action: str) -> None:
    """The desktop extension never treats a mutable tab-list index as a stable target."""
    parameters: dict[str, object] = {"targetId": "1"}
    if action == "navigate":
        parameters["targetUrl"] = "https://example.com/checkout"

    with pytest.raises(PlaywrightBrowserError, match="tab indices can change"):
        _mcp_calls(action, parameters)


def test_mutable_playwright_tab_index_inside_act_request_is_rejected() -> None:
    """Nested act requests cannot bypass the mutable tab-index guard."""
    parameters: dict[str, object] = {
        "request": {"kind": "click", "ref": "e1", "targetId": "1"},
    }

    with pytest.raises(PlaywrightBrowserError, match="stable page identity"):
        _mcp_calls("act", parameters)


@pytest.mark.asyncio
async def test_status_is_lazy_until_extension_use(tmp_path: Path) -> None:
    """Enabling the capability does not launch or take over a browser before first use."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)

    result = await provider.execute("status", {})

    assert result.payload == {
        "stable_targeting": False,
        "supported_control_actions": ["start", "stop", "open"],
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
        "mindroom.playwright_mcp_session.stdio_client",
        lambda _parameters: _FailingStdioContext(),
    )

    with pytest.raises(PlaywrightBrowserError, match="extension startup failed"):
        await asyncio.wait_for(provider._call_tool("browser_tabs", {"action": "list"}), timeout=0.5)

    assert provider.running is False


@pytest.mark.asyncio
async def test_permanent_close_rejects_calls(tmp_path: Path) -> None:
    """Final closure forbids restarting the shared transport."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    await provider.close()
    with pytest.raises(PlaywrightBrowserError, match="provider is closed"):
        await provider._call_tool("browser_tabs", {"action": "list"})


@pytest.mark.asyncio
async def test_browser_stop_remains_restartable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stop retires a live stdio session; each restart creates a fresh process and session."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node fixture required")
    script = tmp_path / "server.cjs"
    script.write_text("""
require('readline').createInterface({input:process.stdin}).on('line', line => {
  const request = JSON.parse(line);
  let result;
  if (request.method === 'initialize') {
    result = {protocolVersion:'2025-11-25',capabilities:{tools:{}},serverInfo:{name:'fixture',version:'1'}};
  } else if (request.method === 'tools/list') {
    result = {tools:[{name:'browser_tabs',inputSchema:{type:'object'}}]};
  } else if (request.method === 'tools/call') {
    result = {content:[{type:'text',text:String(process.pid)}]};
  }
  if (result) console.log(JSON.stringify({jsonrpc:'2.0',id:request.id,result}));
});
""")
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path, command=node, call_timeout_seconds=5)
    monkeypatch.setattr(provider, "_server_args", lambda: [str(script)])
    previous_session = None
    process_ids: set[str] = set()
    try:
        for _ in range(3):
            started = await provider.execute("start", {})
            session = provider._session
            assert session is not None
            assert session is not previous_session
            assert session.running
            assert provider.running
            process_id = started.payload["result"]
            assert isinstance(process_id, str)
            assert process_id.isdigit()
            assert process_id not in process_ids
            process_ids.add(process_id)
            stopped = await asyncio.wait_for(provider.execute("stop", {}), 6)
            assert stopped.payload["running"] is False
            assert not provider.running
            assert not session.running
            with pytest.raises(RuntimeError, match="session is closed"):
                await session.call_tool("browser_tabs", {"action": "list"})
            previous_session = session
    finally:
        await provider.close()
    with pytest.raises(PlaywrightBrowserError, match="provider is closed"):
        await provider.execute("start", {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "parameters"),
    [
        ("navigate", {"targetUrl": "https://example.com/checkout"}),
        ("close", {}),
        ("focus", {}),
        ("pdf", {}),
        ("upload", {"paths": ["invoice.txt"]}),
        ("dialog", {"accept": True}),
        ("act", {"request": {"kind": "click", "ref": "e3"}}),
        ("act", {"request": {"kind": "evaluate", "fn": "() => document.title"}}),
    ],
)
async def test_existing_page_control_is_rejected_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
    parameters: dict[str, object],
) -> None:
    """No unbound operation may reach whichever tab replaced the observed page."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    call_tool = AsyncMock(return_value=_text_result("would affect replacement"))
    monkeypatch.setattr(provider, "_call_tool", call_tool)
    with pytest.raises(PlaywrightBrowserError, match="stable page identity") as error:
        await provider.execute(action, parameters)
    assert not isinstance(error.value, PlaywrightActionOutcomeUnknownError)
    assert "reconnect" in str(error.value)
    call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_url_and_title_after_replacement_cannot_authorize_a_click(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An indistinguishable tab-list result cannot turn old element refs into authority."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    monkeypatch.setattr(PlaywrightMCPBrowserProvider, "running", property(lambda _self: True))
    observed_tabs = "- 0: (current) [Checkout](https://example.com/checkout)"
    call_tool = AsyncMock(return_value=_text_result(observed_tabs))
    monkeypatch.setattr(provider, "_call_tool", call_tool)
    first = await provider.execute("tabs", {})
    # The old Page closes; a different Page now has the same index, URL and title.
    replacement = await provider.execute("tabs", {})
    assert first.payload["result"] == replacement.payload["result"]
    with pytest.raises(PlaywrightBrowserError, match="stable page identity"):
        await provider.execute("act", {"request": {"kind": "click", "ref": "e3"}})
    assert [call.args[0] for call in call_tool.await_args_list] == ["browser_tabs", "browser_tabs"]


@pytest.mark.asyncio
async def test_provider_reports_stable_targeting_limit_before_start(tmp_path: Path) -> None:
    """Capability discovery truthfully bounds control before launching the extension."""
    provider = PlaywrightMCPBrowserProvider(output_dir=tmp_path)
    status = await provider.execute("status", {})
    assert status.payload["stable_targeting"] is False
    assert status.payload["supported_control_actions"] == ["start", "stop", "open"]
