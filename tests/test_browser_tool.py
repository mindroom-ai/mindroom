"""Tests for OpenClaw-style BrowserTools."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.custom_tools.browser import (
    DEFAULT_AI_SNAPSHOT_MAX_CHARS,
    BrowserTabState,
    BrowserTools,
    _clean_str,
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
    tab = BrowserTabState(target_id="t1", page=SimpleNamespace(), refs={"e1": "#submit"})

    assert BrowserTools._resolve_selector(tab, None) is None
    assert BrowserTools._resolve_selector(tab, "e1") == "#submit"
    assert BrowserTools._resolve_selector(tab, "#explicit") == "#explicit"


def test_resolve_max_chars_behavior() -> None:
    """Snapshot max char resolution handles explicit, efficient, and defaults."""
    assert BrowserTools._resolve_max_chars(max_chars=128, mode=None) == 128
    assert BrowserTools._resolve_max_chars(max_chars=0, mode=None) is None
    assert BrowserTools._resolve_max_chars(max_chars=None, mode="efficient") is None
    assert BrowserTools._resolve_max_chars(max_chars=None, mode=None) == DEFAULT_AI_SNAPSHOT_MAX_CHARS


@pytest.mark.asyncio
async def test_browser_unknown_action_raises() -> None:
    """Unknown browser actions are rejected."""
    tool = BrowserTools()

    with pytest.raises(ValueError, match="Unknown action: nope"):
        await tool.browser(action="nope")


@pytest.mark.asyncio
async def test_browser_open_requires_target_url() -> None:
    """Open action requires targetUrl."""
    tool = BrowserTools()

    with pytest.raises(ValueError, match="targetUrl required for action=open"):
        await tool.browser(action="open")


@pytest.mark.asyncio
async def test_browser_open_dispatches_to_open_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open action routes to _open_tab with normalized profile and url."""
    tool = BrowserTools()
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
    tool = BrowserTools()

    with pytest.raises(ValueError, match="host target only"):
        await tool.browser(action="status", target="sandbox")

    with pytest.raises(ValueError, match="host target only"):
        await tool.browser(action="status", target="node")

    with pytest.raises(ValueError, match="node parameter is not supported in MindRoom"):
        await tool.browser(action="status", target="host", node="node-1")


@pytest.mark.asyncio
async def test_act_unknown_kind_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown act kind is rejected."""
    tool = BrowserTools()
    mock_state = object()
    tab = BrowserTabState(target_id="tab-1", page=SimpleNamespace())

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
    tool = BrowserTools()
    mock_state = object()

    click = AsyncMock()
    first = SimpleNamespace(click=click)
    locator_result = SimpleNamespace(first=first)
    locator = MagicMock(return_value=locator_result)
    page: Any = SimpleNamespace(locator=locator)
    tab = BrowserTabState(target_id="tab-1", page=page, refs={"e1": "#submit"})

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
    tool = BrowserTools()
    mock_state = object()
    page: Any = SimpleNamespace(locator=MagicMock())
    tab = BrowserTabState(target_id="tab-1", page=page, refs={})

    monkeypatch.setattr(tool, "_ensure_profile", AsyncMock(return_value=mock_state))
    monkeypatch.setattr(tool, "_resolve_tab", AsyncMock(return_value=("tab-1", tab)))

    with pytest.raises(ValueError, match="valid ref or selector"):
        await tool._act(
            profile_name="openclaw",
            request={"kind": "fill", "fields": [{"value": "hello"}]},
            fallback_target_id=None,
        )
