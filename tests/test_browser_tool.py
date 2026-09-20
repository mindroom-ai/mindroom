"""Tests for BrowserTools."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shutil
import socket
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from aiohttp import web
from playwright.async_api import Error as PlaywrightError

from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.custom_tools.browser import (
    _DEFAULT_AI_SNAPSHOT_MAX_CHARS,
    BrowserTools,
    _BrowserProfileState,
    _BrowserTabState,
    _clean_str,
    _persistent_launch_kwargs,
    _profile_dir,
)
from mindroom.desktop.protocol import DesktopResponse, EncryptedDesktopMedia
from mindroom.message_target import MessageTarget
from mindroom.server_fetch_url import ServerFetchUrlError
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.worker_computer.protocol import BrowserSession
from mindroom.worker_computer.runtime import WorkerComputerRuntime
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.browser_lifecycle_helpers import LifecycleBrowser
from tests.conftest import make_conversation_reader_mock, make_relation_lookup
from tests.test_worker_computer_runtime import FakeDisplay

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from playwright.async_api import Download as PlaywrightDownload

TEST_RUNTIME_PATHS = resolve_primary_runtime_paths(config_path=Path("config.yaml"))
DESKTOP_MEDIA = EncryptedDesktopMedia(
    url="mxc://example.org/browser",
    key="key",
    iv="iv",
    sha256="hash",
    mime_type="image/png",
    size=8,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("hello", "hello"),
        ("  hello  ", "hello"),
        ("", None),
        ("   ", None),
        (123, None),
        (None, None),
    ],
)
def test_clean_str_normalizes_values(value: object, expected: str | None) -> None:
    """_clean_str strips strings and rejects non-strings."""
    assert _clean_str(value) == expected


def test_profile_dir_distinct_names_yield_distinct_paths(tmp_path: Path) -> None:
    """Different profile names should map to different directories under browser-profiles."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )

    mindroom_dir = _profile_dir(runtime_paths, "mindroom")
    chrome_dir = _profile_dir(runtime_paths, "chrome")
    profiles_root = (runtime_paths.storage_root / "browser-profiles").resolve()

    assert mindroom_dir != chrome_dir
    assert mindroom_dir.parent == profiles_root
    assert chrome_dir.parent == profiles_root


def test_profile_dir_clamps_existing_dir_to_0700(tmp_path: Path) -> None:
    """profile_dir() must clamp permissions even when the dir already exists with looser mode."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )
    target = tmp_path / "browser-profiles" / "mindroom"
    target.mkdir(parents=True)
    target.chmod(0o755)

    result = _profile_dir(runtime_paths, "mindroom")

    assert result == target.resolve()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_persistent_launch_kwargs_runtime_env_wins_over_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit runtime env should beat ambient shell env for browser executable resolution."""
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", "/wrong")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"BROWSER_EXECUTABLE_PATH": "/right"},
    )

    launch_kwargs = _persistent_launch_kwargs(runtime_paths, "mindroom", headless=True)

    assert launch_kwargs["executable_path"] == "/right"
    assert "chromium_sandbox" not in launch_kwargs


def test_validate_target_accepts_none_host_and_desktop() -> None:
    """MindRoom browser target validation accepts both supported runtimes."""
    BrowserTools._validate_target(target=None, node=None)
    BrowserTools._validate_target(target="host", node=None)
    BrowserTools._validate_target(target="desktop", node=None)


def test_validate_target_rejects_invalid_node_and_non_host_targets() -> None:
    """MindRoom browser target validation rejects unsupported modes."""
    with pytest.raises(ValueError, match="node parameter is not supported in MindRoom"):
        BrowserTools._validate_target(target="host", node="node-1")

    with pytest.raises(ValueError, match="does not support sandbox or node"):
        BrowserTools._validate_target(target="sandbox", node=None)

    with pytest.raises(ValueError, match="does not support sandbox or node"):
        BrowserTools._validate_target(target="node", node=None)

    with pytest.raises(ValueError, match="Unsupported target"):
        BrowserTools._validate_target(target="unknown", node=None)


def test_resolve_selector_prefers_ref_mapping() -> None:
    """Refs resolve to selectors and missing refs pass through."""
    tab = _BrowserTabState(target_id="t1", page=SimpleNamespace(), refs={"e1": "#submit"})

    assert BrowserTools._resolve_selector(tab, None) is None
    assert BrowserTools._resolve_selector(tab, "e1") == "#submit"
    assert BrowserTools._resolve_selector(tab, "#explicit") == "#explicit"


def test_resolve_max_chars_behavior() -> None:
    """Snapshot max char resolution handles explicit, efficient, and defaults."""
    assert BrowserTools._resolve_max_chars(max_chars=128, mode=None) == 128
    assert BrowserTools._resolve_max_chars(max_chars=0, mode=None) is None
    assert BrowserTools._resolve_max_chars(max_chars=None, mode="efficient") is None
    assert BrowserTools._resolve_max_chars(max_chars=None, mode=None) == _DEFAULT_AI_SNAPSHOT_MAX_CHARS


def test_resolve_output_dir_defaults_to_runtime_storage_root(tmp_path: Path) -> None:
    """Browser artifacts should default under the committed runtime storage root."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths)

    output_dir = tool._resolve_output_dir()

    assert output_dir == (runtime_paths.storage_root / "browser").resolve()
    assert output_dir.is_dir()


def test_resolve_output_dir_prefers_tool_runtime_context_storage_path(tmp_path: Path) -> None:
    """Live tool context should override the runtime-root default for browser artifacts."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths)
    context_storage_path = tmp_path / "context-storage"
    context = make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id="!room:example.org",
            thread_id=None,
            reply_to_event_id=None,
        ),
        requester_id="@alice:example.org",
        client=MagicMock(),
        config=MagicMock(),
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        storage_path=context_storage_path,
    )

    with tool_runtime_context(context):
        output_dir = tool._resolve_output_dir()

    assert output_dir == (context_storage_path / "browser").resolve()
    assert output_dir.is_dir()


def test_resolve_output_dir_does_not_reuse_previous_context_storage_path(tmp_path: Path) -> None:
    """Reusable browser tools should write artifacts under the active context."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths)

    def runtime_context(storage_path: Path) -> ToolRuntimeContext:
        return make_test_tool_runtime_context(
            agent_name="general",
            target=MessageTarget.resolve(
                room_id="!room:example.org",
                thread_id=None,
                reply_to_event_id=None,
            ),
            requester_id="@alice:example.org",
            client=MagicMock(),
            config=MagicMock(),
            runtime_paths=runtime_paths,
            relations=make_relation_lookup(),
            conversation_reader=make_conversation_reader_mock(),
            storage_path=storage_path,
        )

    first_storage_path = tmp_path / "first-context"
    second_storage_path = tmp_path / "second-context"

    with tool_runtime_context(runtime_context(first_storage_path)):
        assert tool._resolve_output_dir() == (first_storage_path / "browser").resolve()

    with tool_runtime_context(runtime_context(second_storage_path)):
        assert tool._resolve_output_dir() == (second_storage_path / "browser").resolve()


@pytest.mark.asyncio
async def test_browser_unknown_action_raises() -> None:
    """Unknown browser actions are rejected."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)

    with pytest.raises(ValueError, match="Unknown action: nope"):
        await tool.browser(action="nope")


@pytest.mark.asyncio
async def test_browser_unknown_action_lists_valid_actions() -> None:
    """Unknown browser actions should point callers at the valid action vocabulary."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)

    with pytest.raises(ValueError, match="Unknown action: click") as exc_info:
        await tool.browser(action="click")

    message = str(exc_info.value)
    assert "Unknown action: click" in message
    assert "Valid actions:" in message
    assert "act" in message
    assert "request.kind='click'" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["help", "actions"])
async def test_browser_discovery_actions_return_action_table(action: str) -> None:
    """The browser tool should expose callable discovery paths."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)

    payload = json.loads(await tool.browser(action=action))

    assert payload["action"] == action
    assert payload["status"] == "ok"
    assert "act" in payload["actions"]
    assert "help" in payload["actions"]
    assert "click" in payload["actKinds"]
    assert "evaluate" in payload["actKinds"]
    assert any(entry["action"] == "act" for entry in payload["actionTable"])


@pytest.mark.asyncio
async def test_browser_discovery_distinguishes_host_and_desktop_semantics() -> None:
    """Discovery must not direct desktop agents toward rejected host-only arguments."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)

    payload = json.loads(await tool.browser(action="help", target="desktop"))
    descriptions = {entry["action"]: entry["description"] for entry in payload["actionTable"]}

    assert "Host target only" in descriptions["focus"]
    for action in ("close", "navigate", "upload", "dialog", "act", "pdf"):
        assert "Host target only" in descriptions[action]
    assert "stable targeting" in descriptions["act"]
    assert "desktop removes its transient scratch file" in descriptions["screenshot"]


def test_browser_function_schema_documents_actions_and_act_request() -> None:
    """Tool schema should make browser actions and act request kinds discoverable."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)

    parameters = tool.async_functions["browser_control"].parameters
    properties = parameters["properties"]

    action_schema = properties["action"]
    assert "act" in action_schema["enum"]
    assert "help" in action_schema["enum"]

    request_description = properties["request"]["description"]
    assert "request.kind" in request_description
    assert "click" in request_description
    assert "evaluate" in request_description
    assert "start, stop, open" in properties["target"]["description"]
    assert "stable targeting" in properties["target"]["description"]
    for field_name in ("compact", "frame", "interactive", "labels", "limit", "mode", "refs", "snapshotFormat"):
        assert "Host-target" in properties[field_name]["description"]


def test_browser_schema_description_requires_registered_browser_function() -> None:
    """BrowserTools should fail fast if the browser entrypoint is missing."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)
    function = tool.async_functions.pop("browser_control")
    try:
        with pytest.raises(RuntimeError, match="Browser function was not registered"):
            tool._describe_browser_schema()
    finally:
        tool.async_functions["browser_control"] = function


def test_browser_docstring_lists_discovery_actions() -> None:
    """The source docstring should match the browser action vocabulary."""
    assert BrowserTools.browser.__doc__ is not None
    assert "/act/help/actions" in BrowserTools.browser.__doc__


def test_browser_metadata_documents_default_output_dir() -> None:
    """Dashboard metadata should mention where screenshots/PDFs land by default."""
    output_dir_field = next(field for field in TOOL_METADATA["browser"].config_fields if field.name == "output_dir")

    assert output_dir_field.description is not None
    assert "host target" in output_dir_field.description
    assert "storage path's browser/ directory" in output_dir_field.description
    assert "desktop-browser" in output_dir_field.description


def test_browser_private_network_metadata_defaults_to_false() -> None:
    """Browser local-network opt-in should expose an explicit secure default."""
    fields = {field.name: field for field in TOOL_METADATA["browser"].config_fields or []}

    assert fields["allow_private_networks"].default is False


def test_browser_docs_list_discovery_actions() -> None:
    """Tool docs should expose the callable discovery actions."""
    docs = Path("docs/tools/web-scraping-and-browser.md").read_text(encoding="utf-8")

    assert "`help`" in docs
    assert "`actions`" in docs


@pytest.mark.asyncio
async def test_browser_open_requires_target_url() -> None:
    """Open action requires targetUrl."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)

    with pytest.raises(ValueError, match="targetUrl required for action=open"):
        await tool.browser(action="open")


@pytest.mark.asyncio
async def test_browser_open_dispatches_to_open_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open action routes to _open_tab with normalized profile and url."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)
    open_tab = AsyncMock(
        return_value={
            "action": "open",
            "profile": "mindroom",
            "status": "ok",
            "targetId": "tab-1",
            "title": "Example",
            "url": "https://example.com",
        },
    )
    monkeypatch.setattr(tool, "_open_tab", open_tab)

    raw = await tool.browser(action="open", targetUrl="https://example.com")
    payload = json.loads(raw)

    open_tab.assert_awaited_once_with("mindroom", "https://example.com")
    assert payload["action"] == "open"
    assert payload["status"] == "ok"
    assert payload["targetId"] == "tab-1"


@pytest.mark.asyncio
async def test_browser_open_rejects_localhost_target_url_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser navigation should reject local dev servers before opening a tab by default."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)
    open_tab = AsyncMock()
    monkeypatch.setattr(tool, "_open_tab", open_tab)

    with pytest.raises(ServerFetchUrlError) as exc_info:
        await tool.browser(action="open", targetUrl="http://localhost:5173/")

    assert exc_info.value.reason == "private_hostname"
    open_tab.assert_not_called()


@pytest.mark.asyncio
async def test_browser_open_allows_private_target_url_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser local-network opt-in should allow local dev server tabs."""
    tool = BrowserTools(TEST_RUNTIME_PATHS, allow_private_networks=True)
    open_tab = AsyncMock(
        return_value={
            "action": "open",
            "profile": "mindroom",
            "status": "ok",
            "targetId": "tab-1",
            "title": "Local",
            "url": "http://localhost:5173/",
        },
    )
    monkeypatch.setattr(tool, "_open_tab", open_tab)

    raw = await tool.browser(action="open", targetUrl="http://localhost:5173/")
    payload = json.loads(raw)

    open_tab.assert_awaited_once_with("mindroom", "http://localhost:5173/")
    assert payload["status"] == "ok"


@pytest.mark.asyncio
async def test_browser_navigate_allows_private_target_url_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser local-network opt-in should allow navigating to a local dev server."""
    tool = BrowserTools(TEST_RUNTIME_PATHS, allow_private_networks=True)
    navigate = AsyncMock(
        return_value={
            "action": "navigate",
            "profile": "mindroom",
            "status": "ok",
            "targetId": "tab-1",
            "title": "Local",
            "url": "http://localhost:5173/",
        },
    )
    monkeypatch.setattr(tool, "_navigate", navigate)

    raw = await tool.browser(action="navigate", targetUrl="http://localhost:5173/")
    payload = json.loads(raw)

    navigate.assert_awaited_once_with("mindroom", "http://localhost:5173/", None)
    assert payload["status"] == "ok"


@pytest.mark.asyncio
async def test_browser_navigate_rejects_unsupported_target_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser navigation should reject local-file and non-HTTP URL schemes."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)
    navigate = AsyncMock()
    monkeypatch.setattr(tool, "_navigate", navigate)

    with pytest.raises(ServerFetchUrlError) as exc_info:
        await tool.browser(action="navigate", targetUrl="file:///etc/passwd")

    assert exc_info.value.reason == "unsupported_scheme"
    navigate.assert_not_called()


@pytest.mark.asyncio
async def test_browser_rejects_unsupported_or_unconfigured_targets() -> None:
    """Desktop browser routing requires a pinned device and still rejects node targets."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)

    with pytest.raises(ValueError, match="does not support sandbox or node"):
        await tool.browser(action="status", target="sandbox")

    with pytest.raises(ValueError, match="does not support sandbox or node"):
        await tool.browser(action="status", target="node")

    with pytest.raises(ValueError, match="configured Matrix desktop device identity"):
        await tool.browser(action="status", target="desktop")

    with pytest.raises(ValueError, match="node parameter is not supported in MindRoom"):
        await tool.browser(action="status", target="host", node="node-1")

    configured_tool = BrowserTools(
        TEST_RUNTIME_PATHS,
        device_user_id="@desktop:example.org",
        device_id="DESKTOP",
        device_ed25519="fingerprint",
    )
    with pytest.raises(ValueError, match="requires a live Matrix runtime context"):
        await configured_tool.browser(action="status", target="desktop")


@pytest.mark.asyncio
async def test_desktop_target_routes_snapshot_and_control_over_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """One browser surface selects the Matrix extension backend without changing agent vocabulary."""
    context = SimpleNamespace(
        requester_id="@alice:example.org",
        agent_name="computer",
        client=object(),
    )
    request = AsyncMock(
        side_effect=[
            DesktopResponse(
                request_id="observe",
                session_id="session",
                ok=True,
                result={"action": "snapshot", "provider": "playwright_mcp_extension", "result": "plain-tree"},
            ),
            DesktopResponse(
                request_id="navigate",
                session_id="session",
                ok=True,
                result={"action": "navigate", "provider": "playwright_mcp_extension", "result": "navigated"},
            ),
            DesktopResponse(
                request_id="click",
                session_id="session",
                ok=True,
                result={"action": "act", "provider": "playwright_mcp_extension", "result": "clicked"},
            ),
        ],
    )
    monkeypatch.setattr("mindroom.custom_tools.browser.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.browser.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = BrowserTools(
        TEST_RUNTIME_PATHS,
        default_target="desktop",
        device_user_id="@desktop:example.org",
        device_id="DESKTOP",
        device_ed25519="fingerprint",
    )

    plain_snapshot = await tool.browser(action="snapshot", maxChars=1000)
    navigate = await tool.browser(action="navigate", targetUrl="https://example.com")
    click = await tool.browser(action="act", request={"kind": "click", "ref": "e3"})

    assert isinstance(plain_snapshot, str)
    assert isinstance(navigate, str)
    assert json.loads(navigate)["provider"] == "playwright_mcp_extension"
    assert isinstance(click, str)
    observe_command = request.await_args_list[0].args[1]
    navigate_command = request.await_args_list[1].args[1]
    click_command = request.await_args_list[2].args[1]
    assert observe_command.action == "browser_observe"
    assert observe_command.parameters == {
        "browser_action": "snapshot",
        "browser_parameters": {"maxChars": 1000},
    }
    assert navigate_command.action == "browser_control"
    assert navigate_command.parameters == {
        "browser_action": "navigate",
        "browser_parameters": {"targetUrl": "https://example.com"},
    }
    assert click_command.action == "browser_control"
    assert click_command.parameters == {
        "browser_action": "act",
        "browser_parameters": {"request": {"kind": "click", "ref": "e3"}},
    }
    assert (observe_command.sequence, navigate_command.sequence, click_command.sequence) == (0, 1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "arguments", "expected"),
    [
        ("snapshot", {"targetId": "1"}, "does not support targetId"),
        (
            "act",
            {"request": {"kind": "click", "ref": "e1", "targetId": "1"}},
            "does not support request.targetId",
        ),
        ("snapshot", {"snapshotFormat": "aria"}, "does not support: snapshotFormat"),
        ("status", {"profile": "chrome"}, "does not support: profile"),
        ("upload", {"paths": ["invoice.pdf"], "inputRef": "e1"}, "does not support: inputRef"),
        ("upload", {"paths": ["invoice.pdf"], "ref": "e1"}, "does not support ref or element"),
        ("upload", {"paths": ["invoice.pdf"], "element": "File input"}, "does not support ref or element"),
        ("dialog", {"timeoutMs": 1000}, "does not support: timeoutMs"),
        ("focus", {}, "does not support focus"),
    ],
)
async def test_desktop_target_rejects_unsupported_host_arguments(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    arguments: dict[str, object],
    expected: str,
) -> None:
    """Desktop routing fails closed instead of pretending host-only arguments were honored."""
    context = SimpleNamespace(requester_id="@alice:example.org", agent_name="computer", client=object())
    request = AsyncMock()
    monkeypatch.setattr("mindroom.custom_tools.browser.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.browser.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = BrowserTools(
        TEST_RUNTIME_PATHS,
        default_target="desktop",
        device_user_id="@desktop:example.org",
        device_id="DESKTOP",
        device_ed25519="fingerprint",
    )

    with pytest.raises(ValueError, match=expected):
        await tool.browser(action=action, **arguments)

    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"interactive": False}, {"compact": False}, {"labels": False}])
async def test_desktop_target_accepts_false_noop_host_hints(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, bool],
) -> None:
    """Explicit false values for host-only hints preserve the desktop default behavior."""
    context = SimpleNamespace(requester_id="@alice:example.org", agent_name="computer", client=object())
    response = DesktopResponse(
        request_id="snapshot",
        session_id="session",
        ok=True,
        result={"action": "snapshot", "provider": "playwright_mcp_extension"},
    )
    request = AsyncMock(return_value=response)
    monkeypatch.setattr("mindroom.custom_tools.browser.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.browser.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = BrowserTools(
        TEST_RUNTIME_PATHS,
        default_target="desktop",
        device_user_id="@desktop:example.org",
        device_id="DESKTOP",
        device_ed25519="fingerprint",
    )

    result = await tool.browser(action="snapshot", **arguments)

    assert json.loads(result) == {"action": "snapshot", "provider": "playwright_mcp_extension"}
    command = request.await_args.args[1]
    assert command.action == "browser_observe"
    assert command.parameters == {"browser_action": "snapshot", "browser_parameters": {}}


@pytest.mark.asyncio
async def test_desktop_target_returns_decrypted_browser_screenshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent receives the real-profile page screenshot as model-visible image media."""
    context = SimpleNamespace(requester_id="@alice:example.org", agent_name="computer", client=object())
    response = DesktopResponse(
        request_id="screenshot",
        session_id="session",
        ok=True,
        result={"action": "screenshot", "provider": "playwright_mcp_extension"},
        screenshot=DESKTOP_MEDIA,
    )
    monkeypatch.setattr("mindroom.custom_tools.browser.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.browser.desktop_response_router",
        lambda _client: SimpleNamespace(request=AsyncMock(return_value=response)),
    )
    decrypt = AsyncMock(return_value=b"\x89PNGpage")
    monkeypatch.setattr("mindroom.custom_tools.browser.download_encrypted_screenshot", decrypt)
    tool = BrowserTools(
        TEST_RUNTIME_PATHS,
        device_user_id="@desktop:example.org",
        device_id="DESKTOP",
        device_ed25519="fingerprint",
    )

    result = await tool.browser(action="screenshot", target="desktop")

    assert not isinstance(result, str)
    assert result.images is not None
    assert result.images[0].content == b"\x89PNGpage"


@pytest.mark.asyncio
async def test_desktop_browser_screenshot_can_return_sendable_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-profile screenshots can be resent from encrypted media without a local file."""
    context = SimpleNamespace(
        requester_id="@alice:example.org",
        agent_name="computer",
        client=object(),
        attachment_ids=(),
        runtime_attachment_ids=[],
        runtime_media_attachments={},
    )
    response = DesktopResponse(
        request_id="screenshot",
        session_id="session",
        ok=True,
        result={"action": "screenshot", "provider": "playwright_mcp_extension"},
        screenshot=DESKTOP_MEDIA,
    )
    monkeypatch.setattr("mindroom.custom_tools.browser.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.browser.desktop_response_router",
        lambda _client: SimpleNamespace(request=AsyncMock(return_value=response)),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.browser.download_encrypted_screenshot",
        AsyncMock(return_value=b"\x89PNGpage"),
    )
    tool = BrowserTools(
        TEST_RUNTIME_PATHS,
        device_user_id="@desktop:example.org",
        device_id="DESKTOP",
        device_ed25519="fingerprint",
    )

    result = await tool.browser(action="screenshot", target="desktop", returnAttachment=True)

    assert not isinstance(result, str)
    payload = json.loads(result.content)
    attachment_id = payload["attachment_id"]
    assert payload["attachment_lifetime"] == "current_turn"
    assert context.runtime_attachment_ids == [attachment_id]
    attachment = context.runtime_media_attachments[attachment_id]
    assert attachment.url == DESKTOP_MEDIA.url
    assert attachment.filename.endswith(".png")


@pytest.mark.asyncio
async def test_browser_return_attachment_requires_desktop_screenshot() -> None:
    """Host screenshots and non-screenshot actions reject the desktop-only attachment option."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)

    with pytest.raises(ValueError, match="requires target=desktop"):
        await tool.browser(action="screenshot", target="host", returnAttachment=True)
    with pytest.raises(ValueError, match="only supported for action=screenshot"):
        await tool.browser(action="tabs", target="desktop", returnAttachment=True)


@pytest.mark.asyncio
async def test_act_unknown_kind_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown act kind is rejected."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)
    mock_state = object()
    tab = _BrowserTabState(target_id="tab-1", page=SimpleNamespace())

    monkeypatch.setattr(tool, "_ensure_profile", AsyncMock(return_value=mock_state))
    monkeypatch.setattr(tool, "_resolve_tab", AsyncMock(return_value=("tab-1", tab)))

    with pytest.raises(ValueError, match="Unsupported act kind: unknown"):
        await tool._act(
            profile_name="mindroom",
            request={"kind": "unknown"},
            fallback_target_id=None,
        )


@pytest.mark.asyncio
async def test_act_click_uses_resolved_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Click act resolves refs and forwards click kwargs to Playwright."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)
    mock_state = object()

    click = AsyncMock()
    first = SimpleNamespace(click=click)
    locator_result = SimpleNamespace(first=first)
    locator = MagicMock(return_value=locator_result)
    page: Any = SimpleNamespace(locator=locator)
    tab = _BrowserTabState(target_id="tab-1", page=page, refs={"e1": "#submit"})

    ensure_profile = AsyncMock(return_value=mock_state)
    resolve_tab = AsyncMock(return_value=("tab-1", tab))
    monkeypatch.setattr(tool, "_ensure_profile", ensure_profile)
    monkeypatch.setattr(tool, "_resolve_tab", resolve_tab)

    payload = await tool._act(
        profile_name="mindroom",
        request={
            "kind": "click",
            "ref": "e1",
            "doubleClick": True,
            "button": "right",
            "modifiers": ["Alt"],
        },
        fallback_target_id="fallback-tab",
    )

    ensure_profile.assert_awaited_once_with("mindroom")
    resolve_tab.assert_awaited_once_with(mock_state, "fallback-tab")
    locator.assert_called_once_with("#submit")
    click.assert_awaited_once_with(button="right", click_count=2, modifiers=["Alt"])
    assert payload["action"] == "act"
    assert payload["kind"] == "click"
    assert payload["status"] == "ok"
    assert payload["targetId"] == "tab-1"


def _install_upload_tab(tool: BrowserTools, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    set_input_files = AsyncMock()
    locator = MagicMock(return_value=SimpleNamespace(first=SimpleNamespace(set_input_files=set_input_files)))
    page: Any = SimpleNamespace(locator=locator)
    tab = _BrowserTabState(target_id="tab-1", page=page, refs={"e1": "input[type=file]"})

    monkeypatch.setattr(tool, "_ensure_profile", AsyncMock(return_value=object()))
    monkeypatch.setattr(tool, "_resolve_tab", AsyncMock(return_value=("tab-1", tab)))
    return set_input_files


@pytest.mark.asyncio
async def test_browser_upload_rejects_paths_outside_upload_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Browser uploads should not read arbitrary local files outside upload roots."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")
    tool = BrowserTools(runtime_paths)
    set_input_files = _install_upload_tab(tool, monkeypatch)

    with pytest.raises(ValueError, match="outside browser upload root"):
        await tool._upload(
            profile_name="mindroom",
            target_id=None,
            paths=[str(outside_file)],
            ref="e1",
            input_ref=None,
            element=None,
            timeout_ms=None,
        )

    set_input_files.assert_not_called()


@pytest.mark.asyncio
async def test_browser_upload_allows_paths_inside_tool_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Browser uploads should allow files produced inside the active tool storage root."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    allowed_file = runtime_paths.storage_root / "browser" / "upload.txt"
    allowed_file.parent.mkdir(parents=True)
    allowed_file.write_text("upload", encoding="utf-8")
    tool = BrowserTools(runtime_paths)
    set_input_files = _install_upload_tab(tool, monkeypatch)

    payload = await tool._upload(
        profile_name="mindroom",
        target_id=None,
        paths=[str(allowed_file)],
        ref="e1",
        input_ref=None,
        element=None,
        timeout_ms=None,
    )

    set_input_files.assert_awaited_once_with([str(allowed_file)], timeout=30_000)
    assert payload["paths"] == [str(allowed_file)]


def test_browser_upload_roots_do_not_reuse_previous_context_output_dir(tmp_path: Path) -> None:
    """Reusable browser tools should not let later calls upload files from a prior context."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths)

    def runtime_context(storage_path: Path) -> ToolRuntimeContext:
        return make_test_tool_runtime_context(
            agent_name="general",
            target=MessageTarget.resolve(
                room_id="!room:example.org",
                thread_id=None,
                reply_to_event_id=None,
            ),
            requester_id="@alice:example.org",
            client=MagicMock(),
            config=MagicMock(),
            runtime_paths=runtime_paths,
            relations=make_relation_lookup(),
            conversation_reader=make_conversation_reader_mock(),
            storage_path=storage_path,
        )

    first_storage_path = tmp_path / "first-context"
    second_storage_path = tmp_path / "second-context"
    first_file = first_storage_path / "browser" / "artifact.txt"
    first_file.parent.mkdir(parents=True)
    first_file.write_text("from first context", encoding="utf-8")

    with tool_runtime_context(runtime_context(first_storage_path)):
        assert tool._resolve_output_dir() == first_file.parent.resolve()

    with (
        tool_runtime_context(runtime_context(second_storage_path)),
        pytest.raises(ValueError, match="outside browser upload root"),
    ):
        tool._resolve_upload_path(str(first_file))


@pytest.mark.asyncio
async def test_browser_upload_rejects_runtime_storage_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Browser uploads should only read browser artifacts, not all runtime state."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    secret_file = runtime_paths.storage_root / "credentials" / "secret.json"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("secret", encoding="utf-8")
    tool = BrowserTools(runtime_paths)
    set_input_files = _install_upload_tab(tool, monkeypatch)

    with pytest.raises(ValueError, match="outside browser upload root"):
        await tool._upload(
            profile_name="mindroom",
            target_id=None,
            paths=[str(secret_file)],
            ref="e1",
            input_ref=None,
            element=None,
            timeout_ms=None,
        )

    set_input_files.assert_not_called()


@pytest.mark.asyncio
async def test_act_fill_requires_at_least_one_valid_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fill act fails when no field resolves to a usable selector."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)
    mock_state = object()
    page: Any = SimpleNamespace(locator=MagicMock())
    tab = _BrowserTabState(target_id="tab-1", page=page, refs={})

    monkeypatch.setattr(tool, "_ensure_profile", AsyncMock(return_value=mock_state))
    monkeypatch.setattr(tool, "_resolve_tab", AsyncMock(return_value=("tab-1", tab)))

    with pytest.raises(ValueError, match="valid ref or selector"):
        await tool._act(
            profile_name="mindroom",
            request={"kind": "fill", "fields": [{"value": "hello"}]},
            fallback_target_id=None,
        )


class _FakePage:
    def is_closed(self) -> bool:
        return False

    def on(self, _event: str, _callback: object) -> None:
        return None


class _FakeContext:
    def __init__(self, *, pages: list[_FakePage] | None = None) -> None:
        self.pages = list(pages or [])
        self.fresh_page = _FakePage()
        self.new_page = AsyncMock(return_value=self.fresh_page)
        self.route = AsyncMock()
        self.close = AsyncMock()
        self.on = MagicMock()


@pytest_asyncio.fixture
async def local_preview_server() -> AsyncIterator[tuple[int, str, list[str]]]:
    """Serve a real loopback app and an observable forbidden redirect destination."""
    hits: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        private_host = probe.getsockname()[0]
    address = ipaddress.ip_address(private_host)
    if not address.is_private or address.is_loopback:
        pytest.skip("A private interface is required for the redirect regression")

    async def serve(request: web.Request) -> web.Response:
        hits.append(request.path)
        if request.path == "/redirect":
            location = "/preview"
            raise web.HTTPFound(location)
        if request.path == "/private-redirect":
            location = f"http://{private_host}:{private_port}/blocked"
            raise web.HTTPFound(location)
        if request.path == "/alias-redirect":
            location = f"http://localhost.localdomain:{private_port}/blocked"
            raise web.HTTPFound(location)
        if request.path == "/metadata-redirect":
            location = "http://169.254.169.254/blocked"
            raise web.HTTPFound(location)
        if request.path == "/app.js":
            return web.Response(text="document.title = 'Preview loaded';", content_type="text/javascript")
        return web.Response(text='<script src="/app.js"></script><h1>Local preview</h1>', content_type="text/html")

    app = web.Application()
    app.router.add_get("/{path:.*}", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    try:
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        private_site = web.TCPSite(runner, private_host, 0)
        await private_site.start()
        assert private_site._server is not None
        private_port = private_site._server.sockets[0].getsockname()[1]
        yield port, private_host, hits
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["computer", "headless", "unbound"])
@pytest.mark.parametrize(("host", "action"), [("127.0.0.1", "open"), ("localhost", "navigate")])
async def test_local_preview_requires_computer_binding(
    binding: str,
    host: str,
    action: str,
    local_preview_server: tuple[int, str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only a dedicated Computer browser opens local pages and their subresources."""
    executable = os.environ.get("MINDROOM_TEST_BROWSER_EXECUTABLE") or shutil.which("chromium")
    if executable is None:
        pytest.skip("Chromium required for local preview integration")
    port, _private_host, hits = local_preview_server
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"BROWSER_EXECUTABLE_PATH": executable},
    )
    tool = BrowserTools(paths)
    # Keep the real Computer binding and all network checks; only the display
    # launch changes so this browser test runs on hosts without Xvnc.
    original_launch = _persistent_launch_kwargs

    def headless_launch(
        runtime_paths: RuntimePaths,
        profile_name: str,
        *,
        headless: bool,
        executable_override: str | None = None,
    ) -> dict[str, Any]:
        assert headless is (binding != "computer")
        return original_launch(runtime_paths, profile_name, headless=True, executable_override=executable_override)

    monkeypatch.setattr("mindroom.custom_tools.browser._persistent_launch_kwargs", headless_launch)
    if binding == "computer":
        tool.bind_worker_display(":99", tmp_path / "workspace")
    elif binding == "headless":
        tool.bind_worker_headless(tmp_path / "workspace", dict(os.environ))
    try:
        url = f"http://{host}:{port}/redirect"
        if binding != "computer":
            with pytest.raises(ServerFetchUrlError):
                await tool.browser(action=action, targetUrl=url)
            assert not hits
        else:
            result = json.loads(await tool.browser(action=action, targetUrl=url))
            assert result["title"] == "Preview loaded"
            assert result["url"] == f"http://{host}:{port}/preview"
            assert "/app.js" in hits
            with pytest.raises(PlaywrightError, match="ERR_SOCKS_CONNECTION_FAILED"):
                await tool.browser(action="open", targetUrl=f"http://{host}:{port}/private-redirect")
            assert "/private-redirect" in hits
            assert "/blocked" not in hits
        for denied_host in ["10.0.0.1", "169.254.169.254"]:
            with pytest.raises(ServerFetchUrlError):
                await tool.browser(action="open", targetUrl=f"http://{denied_host}/")
    finally:
        await tool.aclose()


@pytest.mark.asyncio
async def test_computer_browser_upstream_preserves_http_and_local_preview(  # noqa: PLR0915 - complete browser/proxy lifecycle
    local_preview_server: tuple[int, str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Chromium owns proxy transport; redirects cannot bypass its upstream policy."""
    executable = os.environ.get("MINDROOM_TEST_BROWSER_EXECUTABLE") or shutil.which("chromium")
    if executable is None:
        pytest.skip("Chromium required for proxy integration")
    requests: list[bytes] = []

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            requests.append(headers.split(b"\r\n", 1)[0])
            if headers.startswith(b"GET http://8.8.8.8/preview "):
                body = b"<title>Forwarded HTTP</title>"
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            else:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    proxy = await asyncio.start_server(upstream, "127.0.0.1", 0)
    monkeypatch.setenv("all_proxy", f"http://127.0.0.1:{proxy.sockets[0].getsockname()[1]}")
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"BROWSER_EXECUTABLE_PATH": executable},
    )
    original_launch = _persistent_launch_kwargs

    def headless_launch(
        runtime_paths: RuntimePaths,
        profile_name: str,
        *,
        headless: bool,
        executable_override: str | None = None,
    ) -> dict[str, Any]:
        assert not headless
        options = original_launch(runtime_paths, profile_name, headless=True, executable_override=executable_override)
        options.setdefault("args", []).append(f"--host-resolver-rules=MAP localhost.localdomain {private_host}")
        return options

    monkeypatch.setattr("mindroom.custom_tools.browser._persistent_launch_kwargs", headless_launch)
    tool = BrowserTools(paths)
    tool.bind_worker_display(":99", tmp_path / "workspace")
    port, private_host, hits = local_preview_server
    try:
        for host in ["localhost", "127.0.0.1", "[::ffff:127.0.0.1]", "localhost."]:
            result = json.loads(await tool.browser(action="open", targetUrl=f"http://{host}:{port}/redirect"))
            assert result["title"] == "Preview loaded"
        assert "/app.js" in hits
        assert not any(b"localhost" in request or b"127.0.0.1" in request for request in requests)
        result = json.loads(await tool.browser(action="open", targetUrl="http://8.8.8.8/preview"))
        assert result["title"] == "Forwarded HTTP"
        assert b"GET http://8.8.8.8/preview HTTP/1.1" in requests
        with pytest.raises(PlaywrightError, match="ERR_TUNNEL_CONNECTION_FAILED"):
            await tool.browser(action="open", targetUrl="https://8.8.8.8/denied")
        assert b"CONNECT 8.8.8.8:443 HTTP/1.1" in requests
        with pytest.raises(PlaywrightError, match="ERR_HTTP_RESPONSE_CODE_FAILURE"):
            await tool.browser(action="open", targetUrl=f"http://localhost:{port}/metadata-redirect")
        assert b"GET http://169.254.169.254/blocked HTTP/1.1" in requests
        with pytest.raises(PlaywrightError, match="ERR_HTTP_RESPONSE_CODE_FAILURE"):
            await tool.browser(action="open", targetUrl=f"http://localhost:{port}/alias-redirect")
        assert any(request.startswith(b"GET http://localhost.localdomain:") for request in requests)
        assert "/blocked" not in hits
        with pytest.raises(ServerFetchUrlError):
            await tool.browser(action="open", targetUrl=f"http://localhost.localdomain:{port}/preview")
    finally:
        await tool.aclose()
        proxy.close()
        await proxy.wait_closed()


def _install_fake_persistent_playwright(
    monkeypatch: pytest.MonkeyPatch,
    *,
    context: _FakeContext,
) -> tuple[dict[str, object], Any]:
    launch_kwargs: dict[str, object] = {}

    class _FakeChromium:
        async def launch_persistent_context(self, **kwargs: object) -> _FakeContext:
            launch_kwargs.update(kwargs)
            return context

    class _FakePlaywright:
        def __init__(self) -> None:
            self.chromium = _FakeChromium()
            self.stop = AsyncMock()

    playwright = _FakePlaywright()

    class _FakePlaywrightStarter:
        async def start(self) -> _FakePlaywright:
            return playwright

    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: _FakePlaywrightStarter())
    return launch_kwargs, playwright


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["close", "launch_failure", "cancel_launch", "close_failure"])
async def test_computer_browser_owns_destination_proxy_lifetime(
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No localhost proxy listener survives browser close or interrupted startup."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    tool = BrowserTools(paths)
    tool.bind_worker_display(":99", tmp_path / "workspace")
    adapter = LifecycleBrowser(pause_at="launch" if outcome == "cancel_launch" else None)
    endpoint = None
    original_launch = adapter.launch_persistent_context

    async def launch(**kwargs: object) -> LifecycleBrowser:
        nonlocal endpoint
        proxy = kwargs.get("proxy")
        assert isinstance(proxy, dict), "Computer browser must enforce TCP destinations"
        endpoint = urlsplit(proxy["server"])
        _reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
        writer.close()
        await writer.wait_closed()
        if outcome == "launch_failure":
            msg = "fixture launch failed"
            raise RuntimeError(msg)
        return await original_launch(**kwargs)

    monkeypatch.setattr(adapter, "launch_persistent_context", launch)
    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: adapter)
    opening = asyncio.create_task(tool.browser("start"))
    try:
        if outcome == "cancel_launch":
            await asyncio.wait_for(adapter.reached.wait(), 2)
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening
        elif outcome == "launch_failure":
            with pytest.raises(RuntimeError, match="fixture launch failed"):
                await opening
        else:
            await opening
            if outcome == "close_failure":
                monkeypatch.setattr(
                    adapter,
                    "close",
                    AsyncMock(side_effect=[RuntimeError("fixture close failed"), None]),
                )
                with pytest.raises(ExceptionGroup, match="Failed to close browser profiles"):
                    await tool.aclose()
            else:
                await tool.aclose()
        assert endpoint is not None
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection(endpoint.hostname, endpoint.port)
    finally:
        opening.cancel()
        await asyncio.gather(opening, return_exceptions=True)
        await tool.aclose()


@pytest.mark.asyncio
async def test_ensure_profile_clears_dead_lock_before_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The action browser retains conservative recovery at its launch boundary."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    profile = runtime_paths.storage_root / "browser-profiles" / "mindroom"
    profile.mkdir(parents=True)
    lock = profile / "SingletonLock"
    lock.symlink_to("old-worker-999999999")
    cookies = profile / "Cookies"
    cookies.write_bytes(b"saved-login")
    context = _FakeContext(pages=[])
    _launch_kwargs, playwright = _install_fake_persistent_playwright(monkeypatch, context=context)

    async def launch(**kwargs: object) -> _FakeContext:
        assert kwargs["user_data_dir"] == str(profile)
        assert not lock.is_symlink()
        assert cookies.read_bytes() == b"saved-login"
        return context

    monkeypatch.setattr(playwright.chromium, "launch_persistent_context", launch)
    tool = BrowserTools(runtime_paths)
    await tool._ensure_profile("mindroom")
    assert cookies.read_bytes() == b"saved-login"


@pytest.mark.asyncio
async def test_ensure_profile_uses_runtime_browser_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Browser startup should honor the executable configured in the explicit runtime."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"BROWSER_EXECUTABLE_PATH": "/opt/custom-browser"},
    )
    tool = BrowserTools(runtime_paths)
    context = _FakeContext(pages=[])
    launch_kwargs, _playwright = _install_fake_persistent_playwright(monkeypatch, context=context)

    state = await tool._ensure_profile("mindroom")

    assert launch_kwargs["headless"] is True
    assert launch_kwargs["service_workers"] == "block"
    assert launch_kwargs["user_data_dir"] == str(runtime_paths.storage_root / "browser-profiles" / "mindroom")
    assert launch_kwargs["viewport"] == {"height": 720, "width": 1280}
    assert launch_kwargs["executable_path"] == "/opt/custom-browser"
    context.new_page.assert_awaited_once_with()
    assert state.active_target_id is not None


@pytest.mark.asyncio
async def test_ensure_profile_installs_server_fetch_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Browser contexts should validate every routed network request URL."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths)
    context = _FakeContext(pages=[])
    _install_fake_persistent_playwright(monkeypatch, context=context)

    await tool._ensure_profile("mindroom")

    context.route.assert_awaited_once()
    route_pattern, route_handler = context.route.await_args.args
    assert route_pattern == "**/*"
    to_thread_calls = 0

    async def fake_to_thread(function: Callable[..., object], *args: object, **kwargs: object) -> object:
        nonlocal to_thread_calls
        to_thread_calls += 1
        return function(*args, **kwargs)

    monkeypatch.setattr("mindroom.browser_fetch_guard.asyncio.to_thread", fake_to_thread)

    unsafe_route = SimpleNamespace(
        request=SimpleNamespace(url="http://127.0.0.1/admin"),
        abort=AsyncMock(),
        continue_=AsyncMock(),
    )
    await route_handler(unsafe_route)

    unsafe_route.abort.assert_awaited_once_with("blockedbyclient")
    unsafe_route.continue_.assert_not_called()
    assert to_thread_calls == 1

    malformed_route = SimpleNamespace(
        request=SimpleNamespace(url="http://[::1"),
        abort=AsyncMock(),
        continue_=AsyncMock(),
    )
    await route_handler(malformed_route)

    malformed_route.abort.assert_awaited_once_with("blockedbyclient")
    malformed_route.continue_.assert_not_called()
    assert to_thread_calls == 2


@pytest.mark.asyncio
async def test_ensure_profile_route_allows_browser_internal_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Browser routing should not block browser-internal non-network URLs."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths)
    context = _FakeContext(pages=[])
    _install_fake_persistent_playwright(monkeypatch, context=context)

    await tool._ensure_profile("mindroom")

    route_handler = context.route.await_args.args[1]
    for internal_url in ("about:blank", "blob:https://example.com/blob-id", "data:text/html,hello"):
        route = SimpleNamespace(
            request=SimpleNamespace(url=internal_url),
            abort=AsyncMock(),
            continue_=AsyncMock(),
        )
        await route_handler(route)

        route.continue_.assert_awaited_once_with()
        route.abort.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_profile_route_allows_private_urls_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Browser route guard should apply the local-network opt-in to subresources."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths, allow_private_networks=True)
    context = _FakeContext(pages=[])
    _install_fake_persistent_playwright(monkeypatch, context=context)

    await tool._ensure_profile("mindroom")

    route_handler = context.route.await_args.args[1]
    local_route = SimpleNamespace(
        request=SimpleNamespace(url="http://localhost:5173/assets/app.js"),
        abort=AsyncMock(),
        continue_=AsyncMock(),
    )
    await route_handler(local_route)

    local_route.continue_.assert_awaited_once_with()
    local_route.abort.assert_not_called()

    metadata_route = SimpleNamespace(
        request=SimpleNamespace(url="http://169.254.169.254/latest/meta-data/"),
        abort=AsyncMock(),
        continue_=AsyncMock(),
    )
    await route_handler(metadata_route)

    metadata_route.abort.assert_awaited_once_with("blockedbyclient")
    metadata_route.continue_.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_profile_creates_user_data_dir_on_disk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Profile startup should create the persistent user-data directory eagerly."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths)
    context = _FakeContext(pages=[])
    launch_kwargs, _playwright = _install_fake_persistent_playwright(monkeypatch, context=context)

    await tool._ensure_profile("mindroom")

    user_data_dir = Path(str(launch_kwargs["user_data_dir"]))
    assert user_data_dir.is_dir()
    assert stat.S_IMODE(user_data_dir.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_ensure_profile_rewrites_playwright_browser_revision_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Playwright binary revision mismatches should produce actionable MindRoom guidance."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths)
    playwright_message = (
        "Executable doesn't exist at "
        "/home/alice/.cache/ms-playwright/chromium_headless_shell-1208/chrome-linux/headless_shell\n"
        "╔════════════════════════════════════════════════════════════╗\n"
        "║ Looks like Playwright was just installed or updated.       ║\n"
        "║ Please run the following command to download new browsers: ║\n"
        "║                                                            ║\n"
        "║     playwright install                                     ║\n"
        "╚════════════════════════════════════════════════════════════╝"
    )

    class _FakeChromium:
        async def launch_persistent_context(self, **_kwargs: object) -> _FakeContext:
            raise PlaywrightError(playwright_message)

    class _FakePlaywright:
        def __init__(self) -> None:
            self.chromium = _FakeChromium()
            self.stop = AsyncMock()

    playwright = _FakePlaywright()

    class _FakePlaywrightStarter:
        async def start(self) -> _FakePlaywright:
            return playwright

    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: _FakePlaywrightStarter())

    with pytest.raises(RuntimeError) as exc_info:
        await tool._ensure_profile("mindroom")

    message = str(exc_info.value)
    assert "chromium_headless_shell-1208" in message
    assert "uv run playwright install chromium" in message
    assert "Looks like Playwright was just installed or updated" not in message
    playwright.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_ensure_profile_uses_storage_root_browser_profiles_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Persistent profiles should live under the runtime storage root."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "custom-storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths)
    context = _FakeContext(pages=[])
    launch_kwargs, _playwright = _install_fake_persistent_playwright(monkeypatch, context=context)

    await tool._ensure_profile("chrome")

    assert launch_kwargs["user_data_dir"] == str(runtime_paths.storage_root / "browser-profiles" / "chrome")


@pytest.mark.asyncio
async def test_ensure_profile_rehydrates_existing_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Persistent startup should register all restored pages and focus the first one."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths)
    page_one = _FakePage()
    page_two = _FakePage()
    context = _FakeContext(pages=[page_one, page_two])
    _launch_kwargs, _playwright = _install_fake_persistent_playwright(monkeypatch, context=context)
    register_tab = MagicMock(side_effect=["tab-1", "tab-2"])
    monkeypatch.setattr(tool, "_register_tab", register_tab)

    state = await tool._ensure_profile("mindroom")

    assert register_tab.call_args_list == [
        ((state, page_one),),
        ((state, page_two),),
    ]
    context.new_page.assert_not_awaited()
    assert state.active_target_id == "tab-1"


@pytest.mark.asyncio
async def test_stop_profile_closes_context_only() -> None:
    """Stopping one profile should close the context and Playwright runtime only."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)
    context = SimpleNamespace(close=AsyncMock())
    playwright = SimpleNamespace(stop=AsyncMock())
    tool._profiles["mindroom"] = _BrowserProfileState(playwright=playwright, context=context)

    await tool._stop_profile("mindroom")

    context.close.assert_awaited_once_with()
    playwright.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_stop_profile_holds_lock_through_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Restarting a profile should wait for shutdown to finish before relaunching Chromium."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = BrowserTools(runtime_paths)
    shutdown_started = asyncio.Event()
    allow_shutdown = asyncio.Event()
    launch_started = asyncio.Event()
    events: list[str] = []

    async def close_context() -> None:
        events.append("close-start")
        shutdown_started.set()
        await allow_shutdown.wait()
        events.append("close-end")

    async def stop_playwright() -> None:
        events.append("stop")

    old_context = _FakeContext(pages=[_FakePage()])
    old_context.close = AsyncMock(side_effect=close_context)
    old_playwright = SimpleNamespace(stop=AsyncMock(side_effect=stop_playwright))
    tool._profiles["mindroom"] = _BrowserProfileState(playwright=old_playwright, context=old_context)

    new_context = _FakeContext(pages=[_FakePage()])

    class _FakeChromium:
        async def launch_persistent_context(self, **_kwargs: object) -> _FakeContext:
            events.append("launch")
            launch_started.set()
            return new_context

    class _FakePlaywright:
        def __init__(self) -> None:
            self.chromium = _FakeChromium()
            self.stop = AsyncMock()

    class _FakePlaywrightStarter:
        async def start(self) -> _FakePlaywright:
            return _FakePlaywright()

    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: _FakePlaywrightStarter())

    stop_task = asyncio.create_task(tool._stop_profile("mindroom"))
    await shutdown_started.wait()

    ensure_task = asyncio.create_task(tool._ensure_profile("mindroom"))
    await asyncio.sleep(0)

    assert not launch_started.is_set()
    assert not ensure_task.done()
    assert events == ["close-start"]

    allow_shutdown.set()
    await stop_task
    await ensure_task

    assert events == ["close-start", "close-end", "stop", "launch"]


@pytest.mark.asyncio
async def test_screenshot_selector_uses_locator_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Selector screenshots should keep using Playwright locator captures."""
    tool = BrowserTools(TEST_RUNTIME_PATHS, output_dir=tmp_path)
    mock_state = object()
    page_screenshot = AsyncMock()
    element_screenshot = AsyncMock()
    locator = MagicMock(return_value=SimpleNamespace(first=SimpleNamespace(screenshot=element_screenshot)))
    page: Any = SimpleNamespace(locator=locator, screenshot=page_screenshot)
    tab = _BrowserTabState(target_id="tab-1", page=page, refs={"e1": "#timeline"})

    monkeypatch.setattr(tool, "_ensure_profile", AsyncMock(return_value=mock_state))
    monkeypatch.setattr(tool, "_resolve_tab", AsyncMock(return_value=("tab-1", tab)))

    payload = await tool._screenshot(
        profile_name="mindroom",
        target_id=None,
        full_page=True,
        ref="e1",
        element=None,
        image_type=None,
    )

    locator.assert_called_once_with("#timeline")
    element_screenshot.assert_awaited_once()
    page_screenshot.assert_not_awaited()
    assert payload["selector"] == "#timeline"


@pytest.mark.asyncio
async def test_worker_display_rejects_desktop_routing_and_binds_outputs(tmp_path: Path) -> None:
    """A managed computer must never escape to another browser target or workspace."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    workspace = tmp_path / "workspace"
    browser.bind_worker_display(":99", workspace)
    with pytest.raises(ValueError, match="desktop"):
        await browser.browser("tabs", target="desktop")
    assert browser._resolve_output_dir() == workspace / "browser"
    await browser.aclose()


def test_worker_display_rejects_output_outside_prepared_workspace(tmp_path: Path) -> None:
    """Authored output paths cannot escape the prepared worker workspace."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths, output_dir=tmp_path / "other")
    with pytest.raises(ValueError, match="workspace"):
        browser.bind_worker_display(":99", tmp_path / "workspace")


@pytest.mark.asyncio
async def test_worker_download_survives_browser_stop(tmp_path: Path) -> None:
    """Download copies live in the prepared workspace after browser context cleanup."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    workspace = tmp_path / "workspace"
    browser.bind_worker_display(":99", workspace)

    class Download:
        """Filesystem-producing download adapter."""

        suggested_filename = "../../document.txt"

        async def save_as(self, path: str | Path) -> None:
            """Persist bytes like Playwright save_as."""
            Path(path).write_text("download bytes")

    await browser._save_worker_download(cast("PlaywrightDownload", Download()))
    await browser.aclose()
    saved = list((workspace / "browser").iterdir())
    assert len(saved) == 1
    assert saved[0].name.endswith("-document.txt")
    assert saved[0].read_text() == "download bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("executable", [None, "/opt/operator-browser"])
async def test_worker_browser_launch_uses_private_display_and_persistent_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executable: str | None,
) -> None:
    """Managed launches use headed Chromium without mutating the parent display environment."""
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "state",
        process_env={"BROWSER_EXECUTABLE_PATH": executable} if executable else {},
    )
    browser = BrowserTools(paths)
    browser.bind_worker_display(":99", tmp_path / "workspace")
    monkeypatch.setenv("DISPLAY", ":42")
    launch, _ = _install_fake_persistent_playwright(monkeypatch, context=_FakeContext())
    await browser._ensure_profile("mindroom")
    assert launch["headless"] is False
    assert launch["executable_path"] == (executable or "/opt/mindroom-browser-mcp/chromium")
    assert launch["chromium_sandbox"] is True
    assert launch["env"]["DISPLAY"] == ":99"
    assert os.environ["DISPLAY"] == ":42"
    assert Path(str(launch["user_data_dir"])) == tmp_path / "state" / "browser-profiles" / "mindroom"
    assert launch["downloads_path"] == str(tmp_path / "workspace" / "browser")
    await browser.aclose()


@pytest.mark.asyncio
async def test_worker_browser_sandbox_failure_does_not_retry_without_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker browser launch must fail closed after one sandboxed attempt."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    browser.bind_worker_display(":99", tmp_path / "workspace")
    launch_calls: list[dict[str, object]] = []
    sandbox_error = "No usable sandbox"

    class _FailingChromium:
        async def launch_persistent_context(self, **kwargs: object) -> _FakeContext:
            launch_calls.append(kwargs)
            raise PlaywrightError(sandbox_error)

    class _FailingPlaywright:
        def __init__(self) -> None:
            self.chromium = _FailingChromium()
            self.stop = AsyncMock()

    playwright = _FailingPlaywright()

    class _FailingStarter:
        async def start(self) -> _FailingPlaywright:
            return playwright

    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: _FailingStarter())

    with pytest.raises(PlaywrightError, match=sandbox_error):
        await browser._ensure_profile("mindroom")

    assert len(launch_calls) == 1
    assert launch_calls[0]["chromium_sandbox"] is True
    playwright.stop.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False])
@pytest.mark.parametrize("action", ["focus", "navigate"])
async def test_selected_worker_tab_is_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
    action: str,
) -> None:
    """Selecting an existing headed tab must change the visible native page."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    if managed:
        browser.bind_worker_display(":99", tmp_path / "workspace")
    adapter = LifecycleBrowser()
    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: adapter)
    try:
        opened = json.loads(await browser.browser("open", targetUrl="https://example.org"))
        fixture_page = adapter.pages[-1]
        fixture_page.foreground = False
        adapter.add_native_page("chrome://newtab")

        selected = json.loads(
            await browser.browser(action, targetId=opened["targetId"], targetUrl="https://example.org/selected"),
        )

        assert selected["targetId"] == opened["targetId"]
        assert fixture_page.foreground is managed
    finally:
        await browser.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["driver_start", "launch", "route", "new_page"])
async def test_worker_cancelled_startup_releases_partial_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Cancellation cannot leave a driver/context outside the persistent owner's cleanup."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    browser.bind_worker_display(":99", tmp_path / "workspace")
    adapter = LifecycleBrowser(pause_at=phase)
    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: adapter)
    runtime = WorkerComputerRuntime(FakeDisplay())

    async def execute() -> object:
        return await browser.browser("start")

    task = asyncio.create_task(
        runtime.run_browser_call("binding", lambda _: BrowserSession(execute, browser.aclose), [], {}),
    )
    await asyncio.wait_for(adapter.reached.wait(), timeout=1)
    task.cancel()
    if phase == "driver_start":
        await asyncio.sleep(0)
        adapter.proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await runtime.close()
    assert adapter.live_resources == set()
    assert json.loads(await browser.browser("status"))["running"] is False


@pytest.mark.asyncio
async def test_native_tabs_and_tool_tabs_share_targets_and_persistent_download_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tabs opened during takeover become usable agent targets without duplicated event hooks."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    workspace = tmp_path / "workspace"
    browser = BrowserTools(paths)
    browser.bind_worker_display(":99", workspace)
    adapter = LifecycleBrowser(initial_pages=True)
    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: adapter)
    runtime = WorkerComputerRuntime(FakeDisplay())

    async def execute(**kwargs: object) -> object:
        return await browser.browser(**kwargs)

    def factory(_display: str) -> BrowserSession:
        return BrowserSession(execute, browser.aclose)

    await runtime.run_browser_call("binding", factory, [], {"action": "start"})
    generation = runtime.status()["generation"]
    await runtime.attach_stream("viewer", generation)
    await runtime.take_control("viewer")
    native = adapter.add_native_page("https://example.org/native")
    await runtime.release_control("viewer")
    tabs = json.loads(await runtime.run_browser_call("binding", factory, [], {"action": "tabs"}))["tabs"]
    assert len(tabs) == 2
    target = next(tab["targetId"] for tab in tabs if tab["url"] == native.url)
    await runtime.run_browser_call(
        "binding",
        factory,
        [],
        {"action": "navigate", "targetId": target, "targetUrl": "https://example.org/resumed"},
    )
    assert native.url == "https://example.org/resumed"
    opened = json.loads(
        await runtime.run_browser_call(
            "binding",
            factory,
            [],
            {"action": "open", "targetUrl": "https://example.org/tool"},
        ),
    )
    tabs = json.loads(await runtime.run_browser_call("binding", factory, [], {"action": "tabs"}))["tabs"]
    assert len(tabs) == 3
    assert sum(tab["targetId"] == opened["targetId"] for tab in tabs) == 1

    class Download:
        """Native download with an observable file result."""

        suggested_filename = "native.txt"

        async def save_as(self, destination: str | Path) -> None:
            """Save downloaded bytes through the real persistent handler."""
            Path(destination).write_text("native download")

    await native.emit("download", Download())
    await adapter.pages[-1].emit("download", Download())
    await runtime.close()
    saved = list((workspace / "browser").iterdir())
    assert len(saved) == 2
    assert all(path.read_text() == "native download" for path in saved)


@pytest.mark.asyncio
async def test_native_page_creation_during_tab_listing_preserves_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native page events during title reads cannot invalidate an in-flight tab iterator."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    browser.bind_worker_display(":99", tmp_path / "workspace")
    adapter = LifecycleBrowser(initial_pages=True)
    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: adapter)
    await browser.browser("start")

    async def title() -> str:
        if len(adapter.pages) == 1:
            adapter.add_native_page("https://example.org/new")
        return "initial page"

    monkeypatch.setattr(adapter.pages[0], "title", title)
    first = json.loads(await browser.browser("tabs"))
    second = json.loads(await browser.browser("tabs"))
    assert len(first["tabs"]) == 1
    assert len(second["tabs"]) == 2
    await browser.aclose()


@pytest.mark.asyncio
async def test_failed_driver_acquisition_closes_manager_owned_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An initial start failure still closes resources acquired by the manager."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    adapter = LifecycleBrowser(fail_start=True)
    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: adapter)
    with pytest.raises(RuntimeError, match="Driver startup failed"):
        await browser.browser("start")
    await browser.aclose()
    assert adapter.live_resources == set()


@pytest.mark.asyncio
async def test_repeated_start_cancellation_keeps_cleanup_owned_until_runtime_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime close waits for acquisition cleanup even after repeated request cancellation."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    browser.bind_worker_display(":99", tmp_path / "workspace")
    adapter = LifecycleBrowser(pause_at="driver_start")
    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: adapter)
    runtime = WorkerComputerRuntime(FakeDisplay())

    closing_browser = asyncio.Event()

    async def close_browser() -> None:
        closing_browser.set()
        await browser.aclose()

    async def execute() -> object:
        return await browser.browser("start")

    task = asyncio.create_task(
        runtime.run_browser_call("binding", lambda _: BrowserSession(execute, close_browser), [], {}),
    )
    await asyncio.wait_for(adapter.reached.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    await asyncio.wait_for(closing_browser.wait(), timeout=1)
    task.cancel()
    closing = asyncio.create_task(runtime.close())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(closing), timeout=0.05)
    assert not adapter.start_cancelled
    adapter.proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(closing, timeout=1)
    assert adapter.live_resources == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["context_close", "driver_stop"])
@pytest.mark.parametrize("profiles", [1, 2])
@pytest.mark.parametrize("entry", ["aclose", "profile_stop", "runtime_stop", "replacement"])
async def test_established_browser_teardown_survives_repeated_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    profiles: int,
    entry: str,
) -> None:
    """Cancelled teardown drains every owned resource before a later restart."""
    initial_tasks = asyncio.all_tasks()
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    adapters = [LifecycleBrowser() for _ in range(profiles)]
    pending = iter(adapters)
    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: next(pending))
    runtime = WorkerComputerRuntime(FakeDisplay())

    async def execute(**kwargs: object) -> object:
        return await browser.browser(**kwargs)

    def factory(_display: str) -> BrowserSession:
        return BrowserSession(execute, browser.aclose)

    for index in range(profiles):
        await runtime.run_browser_call("original", factory, [], {"action": "start", "profile": f"p{index}"})
    adapters[0].pause_at = phase
    if entry == "aclose":
        operation = browser.aclose()
    elif entry == "profile_stop":
        operation = browser.browser("stop", profile="p0")
    elif entry == "runtime_stop":
        operation = runtime.stop()
    else:
        operation = runtime.run_browser_call("replacement", factory, [], {"action": "status"})
    task = asyncio.create_task(operation)
    await asyncio.wait_for(adapters[0].reached.wait(), timeout=1)
    try:
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done(), "Caller escaped before owned teardown completed"
        assert adapters[0].live_resources
    finally:
        adapters[0].proceed.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
    assert task.cancelled()
    assert all(not adapter.live_resources for adapter in adapters)
    assert not browser._profiles
    assert not browser._startup_cleanup_tasks
    await runtime.ensure_started()
    await runtime.close()
    assert not runtime.display.healthy()
    assert not (asyncio.all_tasks() - initial_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["context_close", "driver_stop"])
async def test_cancelled_profile_stop_blocks_replacement_until_resources_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """A cancelled ordinary stop cannot unlock an occupied persistent profile."""
    initial_tasks = asyncio.all_tasks()
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    old, new = LifecycleBrowser(), LifecycleBrowser()
    pending = iter([old, new])
    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: next(pending))
    await browser.browser("start")
    old.pause_at = phase
    stopping = asyncio.create_task(browser.browser("stop"))
    await asyncio.wait_for(old.reached.wait(), timeout=1)
    restarting = asyncio.create_task(browser.browser("start"))
    try:
        for _ in range(3):
            stopping.cancel()
            await asyncio.sleep(0)
        assert not restarting.done()
        assert not new.live_resources, "Replacement acquired resources before old teardown drained"
    finally:
        old.proceed.set()
        await asyncio.gather(stopping, restarting, return_exceptions=True)
        await browser.aclose()
    assert stopping.cancelled()
    assert not old.live_resources
    assert not new.live_resources
    assert not (asyncio.all_tasks() - initial_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["context_close", "driver_stop"])
async def test_browser_cleanup_error_still_drains_other_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """One failing cleanup cannot strand other profiles or forget retry ownership."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    first, second = LifecycleBrowser(), LifecycleBrowser()
    pending = iter([first, second])
    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: next(pending))
    for profile in ["first", "second"]:
        await browser.browser("start", profile=profile)

    async def fail_cleanup(at: str) -> None:
        if at == phase:
            msg = "cleanup failed"
            raise RuntimeError(msg)

    monkeypatch.setattr(first, "checkpoint", fail_cleanup)
    try:
        with pytest.raises(ExceptionGroup, match="browser profiles"):
            await browser.aclose()
        assert not second.live_resources
        assert "first" in browser._profiles
    finally:
        monkeypatch.undo()
        await browser.aclose()
    assert not first.live_resources
    assert not browser._profiles


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["context_close", "driver_stop"])
async def test_failed_profile_teardown_blocks_reuse_and_retries_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Ordinary operations never reuse an invalid context or overlap retained cleanup ownership."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state")
    browser = BrowserTools(paths)
    old, new = LifecycleBrowser(), LifecycleBrowser()
    pending = iter([old, new])
    monkeypatch.setattr("mindroom.custom_tools.browser.async_playwright", lambda: next(pending))
    await browser.browser("start")
    checkpoint = old.checkpoint

    async def fail_cleanup(at: str) -> None:
        if at == phase:
            message = "cleanup failed"
            raise RuntimeError(message)

    old.checkpoint = fail_cleanup
    try:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await browser.browser("stop")
        assert old.live_resources
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await browser.browser("start")
        assert not new.live_resources
        assert json.loads(await browser.browser("status"))["running"] is False
        assert json.loads(await browser.browser("profiles"))["running_profiles"] == []
        old.checkpoint = checkpoint
        old.pause_at = phase
        restarting = asyncio.create_task(browser.browser("start"))
        await asyncio.wait_for(old.reached.wait(), timeout=1)
        replacement = asyncio.create_task(browser.browser("start"))
        try:
            for _ in range(3):
                restarting.cancel()
                await asyncio.sleep(0)
            assert not restarting.done()
            assert not replacement.done()
            assert not new.live_resources
        finally:
            old.proceed.set()
            await asyncio.gather(restarting, replacement, return_exceptions=True)
        assert restarting.cancelled()
        assert not old.live_resources
        assert new.live_resources == {"driver", "context"}
        assert json.loads(await browser.browser("status"))["running"] is True
    finally:
        old.checkpoint = checkpoint
        old.proceed.set()
        await browser.aclose()
    assert not old.live_resources
    assert not new.live_resources
