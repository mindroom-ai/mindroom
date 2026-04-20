"""Tests for OpenClaw-style BrowserTools."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.custom_tools.browser import (
    _DEFAULT_AI_SNAPSHOT_MAX_CHARS,
    BrowserTools,
    _BrowserProfileState,
    _BrowserTabState,
    _clean_str,
    _clear_stale_singleton_locks,
    profile_dir,
)
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import make_conversation_cache_mock, make_event_cache_mock

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

    openclaw_dir = profile_dir(runtime_paths, "openclaw")
    chrome_dir = profile_dir(runtime_paths, "chrome")
    profiles_root = (runtime_paths.storage_root / "browser-profiles").resolve()

    assert openclaw_dir != chrome_dir
    assert openclaw_dir.parent == profiles_root
    assert chrome_dir.parent == profiles_root


def test_clear_stale_singleton_locks_unlinks_stale_symlink(tmp_path: Path) -> None:
    """Stale Chromium singleton lock symlinks should be removed."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    lock = profile_dir / "SingletonLock"
    lock.symlink_to("mindroom-999999999")

    _clear_stale_singleton_locks(profile_dir)

    assert not lock.is_symlink()


def test_clear_stale_singleton_locks_keeps_live_pid_symlink(tmp_path: Path) -> None:
    """Live Chromium singleton lock symlinks should be left in place."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    lock = profile_dir / "SingletonLock"
    lock.symlink_to(f"mindroom-{os.getpid()}")

    _clear_stale_singleton_locks(profile_dir)

    assert lock.is_symlink()


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
            "profile": "openclaw",
            "status": "ok",
            "targetId": "tab-1",
            "title": "Example",
            "url": "https://example.com",
        },
    )
    monkeypatch.setattr(tool, "_open_tab", open_tab)

    raw = await tool.browser(action="open", targetUrl="https://example.com")
    payload = json.loads(raw)

    open_tab.assert_awaited_once_with("openclaw", "https://example.com")
    assert payload["action"] == "open"
    assert payload["status"] == "ok"
    assert payload["targetId"] == "tab-1"


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
            profile_name="openclaw",
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
        profile_name="openclaw",
        request={
            "kind": "click",
            "ref": "e1",
            "doubleClick": True,
            "button": "right",
            "modifiers": ["Alt"],
        },
        fallback_target_id="fallback-tab",
    )

    ensure_profile.assert_awaited_once_with("openclaw")
    resolve_tab.assert_awaited_once_with(mock_state, "fallback-tab")
    locator.assert_called_once_with("#submit")
    click.assert_awaited_once_with(button="right", click_count=2, modifiers=["Alt"])
    assert payload["action"] == "act"
    assert payload["kind"] == "click"
    assert payload["status"] == "ok"
    assert payload["targetId"] == "tab-1"


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
            profile_name="openclaw",
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

    state = await tool._ensure_profile("openclaw")

    assert launch_kwargs["headless"] is True
    assert launch_kwargs["service_workers"] == "block"
    assert launch_kwargs["user_data_dir"] == str(runtime_paths.storage_root / "browser-profiles" / "openclaw")
    assert launch_kwargs["viewport"] == {"height": 720, "width": 1280}
    assert launch_kwargs["executable_path"] == "/opt/custom-browser"
    context.new_page.assert_awaited_once_with()
    assert state.active_target_id is not None


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

    await tool._ensure_profile("openclaw")

    user_data_dir = Path(str(launch_kwargs["user_data_dir"]))
    assert user_data_dir.is_dir()


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

    state = await tool._ensure_profile("openclaw")

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
    tool._profiles["openclaw"] = _BrowserProfileState(playwright=playwright, context=context)

    await tool._stop_profile("openclaw")

    context.close.assert_awaited_once_with()
    playwright.stop.assert_awaited_once_with()


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
        profile_name="openclaw",
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
