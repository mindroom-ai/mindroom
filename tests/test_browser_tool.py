"""Tests for BrowserTools."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.custom_tools.browser import (
    _DEFAULT_AI_SNAPSHOT_MAX_CHARS,
    BrowserTools,
    _BrowserProfileState,
    _BrowserTabState,
    _clean_str,
    _clear_stale_singleton_locks,
    _persistent_launch_kwargs,
    _profile_dir,
)
from mindroom.server_fetch_url import ServerFetchUrlError
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import make_conversation_cache_mock, make_event_cache_mock

if TYPE_CHECKING:
    from collections.abc import Callable

TEST_RUNTIME_PATHS = resolve_primary_runtime_paths(config_path=Path("config.yaml"))


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


def test_clear_stale_singleton_locks_unlinks_stale_symlink(tmp_path: Path) -> None:
    """Stale Chromium singleton lock symlinks should be removed."""
    _profile_dir = tmp_path / "profile"
    _profile_dir.mkdir()
    lock = _profile_dir / "SingletonLock"
    lock.symlink_to("mindroom-999999999")

    _clear_stale_singleton_locks(_profile_dir)

    assert not lock.is_symlink()


def test_clear_stale_singleton_locks_keeps_live_pid_symlink(tmp_path: Path) -> None:
    """Live Chromium singleton lock symlinks should be left in place."""
    _profile_dir = tmp_path / "profile"
    _profile_dir.mkdir()
    lock = _profile_dir / "SingletonLock"
    lock.symlink_to(f"mindroom-{os.getpid()}")

    _clear_stale_singleton_locks(_profile_dir)

    assert lock.is_symlink()


def test_clear_stale_singleton_locks_is_idempotent_for_empty_dir(tmp_path: Path) -> None:
    """The exported singleton-lock cleanup helper should be safe for empty profiles."""
    _profile_dir = tmp_path / "profile"
    _profile_dir.mkdir()

    _clear_stale_singleton_locks(_profile_dir)
    _clear_stale_singleton_locks(_profile_dir)

    assert list(_profile_dir.iterdir()) == []


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


def test_validate_target_accepts_none_and_host() -> None:
    """MindRoom browser target validation accepts host and unset targets."""
    BrowserTools._validate_target(target=None, node=None)
    BrowserTools._validate_target(target="host", node=None)


def test_validate_target_rejects_invalid_node_and_non_host_targets() -> None:
    """MindRoom browser target validation rejects unsupported modes."""
    with pytest.raises(ValueError, match="node parameter is not supported in MindRoom"):
        BrowserTools._validate_target(target="host", node="node-1")

    with pytest.raises(ValueError, match="host target only"):
        BrowserTools._validate_target(target="sandbox", node=None)

    with pytest.raises(ValueError, match="host target only"):
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
    context = ToolRuntimeContext(
        agent_name="general",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        requester_id="@alice:example.org",
        client=MagicMock(),
        config=MagicMock(),
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        conversation_cache=make_conversation_cache_mock(),
        storage_path=context_storage_path,
    )

    with tool_runtime_context(context):
        output_dir = tool._resolve_output_dir()

    assert output_dir == (context_storage_path / "browser").resolve()
    assert output_dir.is_dir()


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


def test_browser_function_schema_documents_actions_and_act_request() -> None:
    """Tool schema should make browser actions and act request kinds discoverable."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)

    parameters = tool.async_functions["browser"].parameters
    properties = parameters["properties"]

    action_schema = properties["action"]
    assert "act" in action_schema["enum"]
    assert "help" in action_schema["enum"]

    request_description = properties["request"]["description"]
    assert "request.kind" in request_description
    assert "click" in request_description
    assert "evaluate" in request_description


def test_browser_schema_description_requires_registered_browser_function() -> None:
    """BrowserTools should fail fast if the browser entrypoint is missing."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)
    function = tool.async_functions.pop("browser")
    try:
        with pytest.raises(RuntimeError, match="Browser function was not registered"):
            tool._describe_browser_schema()
    finally:
        tool.async_functions["browser"] = function


def test_browser_docstring_lists_discovery_actions() -> None:
    """The source docstring should match the browser action vocabulary."""
    assert BrowserTools.browser.__doc__ is not None
    assert "/act/help/actions" in BrowserTools.browser.__doc__


def test_browser_metadata_documents_default_output_dir() -> None:
    """Dashboard metadata should mention where screenshots/PDFs land by default."""
    output_dir_field = next(field for field in TOOL_METADATA["browser"].config_fields if field.name == "output_dir")

    assert output_dir_field.description is not None
    assert "storage path's browser/ directory" in output_dir_field.description


def test_browser_metadata_lists_discovery_actions() -> None:
    """Dashboard metadata should expose the callable discovery actions."""
    description = TOOL_METADATA["browser"].description

    assert "help" in description
    assert "actions" in description


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
async def test_browser_open_rejects_private_target_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser navigation should reject private network targets before opening a tab."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)
    open_tab = AsyncMock()
    monkeypatch.setattr(tool, "_open_tab", open_tab)

    with pytest.raises(ServerFetchUrlError) as exc_info:
        await tool.browser(action="open", targetUrl="http://127.0.0.1:8000/admin")

    assert exc_info.value.reason == "private_address"
    open_tab.assert_not_called()


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
async def test_browser_rejects_non_host_targets() -> None:
    """MindRoom browser currently supports host only."""
    tool = BrowserTools(TEST_RUNTIME_PATHS)

    with pytest.raises(ValueError, match="host target only"):
        await tool.browser(action="status", target="sandbox")

    with pytest.raises(ValueError, match="host target only"):
        await tool.browser(action="status", target="node")

    with pytest.raises(ValueError, match="node parameter is not supported in MindRoom"):
        await tool.browser(action="status", target="host", node="node-1")


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


@pytest.mark.asyncio
async def test_browser_upload_rejects_paths_outside_runtime_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Browser uploads should not read arbitrary local files outside runtime storage."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")
    tool = BrowserTools(runtime_paths)
    mock_state = object()
    set_input_files = AsyncMock()
    locator = MagicMock(return_value=SimpleNamespace(first=SimpleNamespace(set_input_files=set_input_files)))
    page: Any = SimpleNamespace(locator=locator)
    tab = _BrowserTabState(target_id="tab-1", page=page, refs={"e1": "input[type=file]"})

    monkeypatch.setattr(tool, "_ensure_profile", AsyncMock(return_value=mock_state))
    monkeypatch.setattr(tool, "_resolve_tab", AsyncMock(return_value=("tab-1", tab)))

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
    mock_state = object()
    set_input_files = AsyncMock()
    locator = MagicMock(return_value=SimpleNamespace(first=SimpleNamespace(set_input_files=set_input_files)))
    page: Any = SimpleNamespace(locator=locator)
    tab = _BrowserTabState(target_id="tab-1", page=page, refs={"e1": "input[type=file]"})

    monkeypatch.setattr(tool, "_ensure_profile", AsyncMock(return_value=mock_state))
    monkeypatch.setattr(tool, "_resolve_tab", AsyncMock(return_value=("tab-1", tab)))

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
