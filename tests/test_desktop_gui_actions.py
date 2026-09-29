"""Tests for desktop app input actions run through the local GUI provider."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from mindroom.desktop.gui_actions import execute_fallback_control, execute_semantic_control
from mindroom.desktop.protocol import DesktopProtocolError
from tests.desktop_helpers import APP_ID, FakeProvider, _command

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_TARGET = {"app": APP_ID, "state_id": "state-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "parameters", "call"),
    [
        ("click_element", {"element_index": 0}, ("click_element", (APP_ID, "state-1", 0))),
        ("set_value", {"element_index": 1, "value": "text"}, ("set_value", (APP_ID, "state-1", 1, "text"))),
        (
            "scroll_element",
            {"element_index": 2, "direction": "down", "pages": 3},
            ("scroll_element", (APP_ID, "state-1", 2, "down", 3)),
        ),
        (
            "perform_action",
            {"element_index": 0, "action_name": "AXPress"},
            ("perform_action", (APP_ID, "state-1", 0, "AXPress")),
        ),
    ],
)
async def test_semantic_actions_reach_the_provider_with_their_exact_target(
    action: str,
    parameters: dict[str, object],
    call: tuple[str, object],
) -> None:
    """Each semantic action becomes one provider call on the observed app state and element."""
    provider = FakeProvider()
    command = _command(action, parameters={**_TARGET, **parameters})
    await execute_semantic_control(provider, command, app_id=APP_ID, state_id="state-1")
    assert provider.calls == [call]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "parameters", "call"),
    [
        ("click", {"x": 10, "y": 20, "button": "right"}, ("click", (APP_ID, "state-1", 10, 20, "right"))),
        ("click", {"x": 10, "y": 20}, ("click", (APP_ID, "state-1", 10, 20, "left"))),
        (
            "double_click",
            {"x": 10, "y": 20},
            ("double_click", {"app_id": APP_ID, "state_id": "state-1", "x": 10, "y": 20, "button": "left"}),
        ),
        ("hover", {"x": 10, "y": 20}, ("hover", {"app_id": APP_ID, "state_id": "state-1", "x": 10, "y": 20})),
        (
            "drag",
            {"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4},
            (
                "drag",
                {
                    "app_id": APP_ID,
                    "state_id": "state-1",
                    "start_x": 1,
                    "start_y": 2,
                    "end_x": 3,
                    "end_y": 4,
                    "duration_ms": 500,
                },
            ),
        ),
        ("type_text", {"text": "hello"}, ("type_text", (APP_ID, "state-1", "hello"))),
        ("scroll", {"direction": "up", "pages": 2}, ("scroll", (APP_ID, "state-1", "up", 2, None, None))),
        ("scroll", {"direction": "up", "pages": 2, "x": 5, "y": 6}, ("scroll", (APP_ID, "state-1", "up", 2, 5, 6))),
        ("keypress", {"keys": ["command", "a"]}, ("keypress", (APP_ID, "state-1", ["command", "a"]))),
    ],
)
async def test_fallback_inputs_reach_the_provider_with_their_defaults(
    action: str,
    parameters: dict[str, object],
    call: tuple[str, object],
) -> None:
    """Each pointer, text, scroll, or key-chord input becomes one provider call, with its documented defaults."""
    provider = FakeProvider()
    command = _command(action, parameters={**_TARGET, **parameters})
    await execute_fallback_control(provider, command, app_id=APP_ID, state_id="state-1")
    assert provider.calls == [call]


@pytest.mark.asyncio
async def test_bridge_allows_empty_semantic_value_but_rejects_shortcut_chord() -> None:
    """Clearing a field is supported while global keyboard shortcuts stay local-policy errors."""
    provider = FakeProvider()
    set_value = _command("set_value", parameters={**_TARGET, "element_index": 0, "value": ""})
    await execute_semantic_control(provider, set_value, app_id=APP_ID, state_id="state-1")
    assert ("set_value", (APP_ID, "state-1", 0, "")) in provider.calls

    keypress = _command(
        "keypress",
        request_id="request-2",
        sequence=2,
        parameters={**_TARGET, "keys": ["command", "tab"]},
    )
    with pytest.raises(DesktopProtocolError, match="not allowed"):
        await execute_fallback_control(provider, keypress, app_id=APP_ID, state_id="state-1")
    assert all(call[0] != "keypress" for call in provider.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("keys", "error"),
    [
        ("a", "Desktop parameter keys must contain a safe app-local key chord."),
        (["command", 1], "Desktop parameter keys must contain a safe app-local key chord."),
    ],
)
async def test_keypress_requires_a_list_of_key_names(keys: object, error: str) -> None:
    """A key chord must be a list of key names before it is normalized."""
    provider = FakeProvider()
    command = _command("keypress", parameters={**_TARGET, "keys": keys})
    with pytest.raises(DesktopProtocolError, match=f"^{re.escape(error)}$"):
        await execute_fallback_control(provider, command, app_id=APP_ID, state_id="state-1")
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "action", "parameters", "error"),
    [
        (execute_semantic_control, "click_element", {"element_index": 0, "x": 1}, "Unexpected desktop parameters: x."),
        (
            execute_semantic_control,
            "click_element",
            {"element_index": True},
            "Desktop parameter element_index must be an integer.",
        ),
        (execute_semantic_control, "click", {"x": 1, "y": 2}, "Unsupported semantic desktop action: click."),
        (
            execute_fallback_control,
            "hover",
            {"x": 1, "y": 2, "button": "left"},
            "Unexpected desktop parameters: button.",
        ),
        (execute_fallback_control, "type_text", {"text": ""}, "Desktop parameter text must be a non-empty string."),
        (
            execute_fallback_control,
            "click_element",
            {"element_index": 0},
            "Unsupported fallback desktop action: click_element.",
        ),
    ],
)
async def test_app_inputs_reject_unrelated_malformed_or_misrouted_commands(
    run: Callable[..., Awaitable[None]],
    action: str,
    parameters: dict[str, object],
    error: str,
) -> None:
    """Strict parameters and the semantic/fallback split are checked before the provider is touched."""
    provider = FakeProvider()
    command = _command(action, parameters={**_TARGET, **parameters})
    with pytest.raises(DesktopProtocolError, match=f"^{re.escape(error)}$"):
        await run(provider, command, app_id=APP_ID, state_id="state-1")
    assert provider.calls == []
