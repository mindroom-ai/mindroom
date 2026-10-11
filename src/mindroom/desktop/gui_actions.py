"""Run desktop app input actions through the local GUI provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from mindroom.desktop.command_parameters import (
    optional_int_parameter,
    optional_str_parameter,
    reject_unexpected_parameters,
    required_int_parameter,
    required_str_parameter,
)
from mindroom.desktop.input import normalize_key_chord
from mindroom.desktop.protocol import DesktopProtocolError

if TYPE_CHECKING:
    from mindroom.desktop.protocol import DesktopCommand
    from mindroom.desktop.provider import DesktopProvider


async def execute_semantic_control(
    provider: DesktopProvider,
    command: DesktopCommand,
    *,
    app_id: str,
    state_id: str,
) -> None:
    """Run one accessibility action on an element of the observed app state."""
    parameters = command.parameters
    if command.action == "click_element":
        reject_unexpected_parameters(parameters, allowed=frozenset({"app", "state_id", "element_index"}))
        await asyncio.to_thread(
            provider.click_element,
            app_id=app_id,
            state_id=state_id,
            element_index=required_int_parameter(parameters, "element_index"),
        )
    elif command.action == "set_value":
        reject_unexpected_parameters(
            parameters,
            allowed=frozenset({"app", "state_id", "element_index", "value"}),
        )
        await asyncio.to_thread(
            provider.set_value,
            app_id=app_id,
            state_id=state_id,
            element_index=required_int_parameter(parameters, "element_index"),
            value=required_str_parameter(parameters, "value", allow_empty=True),
        )
    elif command.action == "scroll_element":
        reject_unexpected_parameters(
            parameters,
            allowed=frozenset({"app", "state_id", "element_index", "direction", "pages"}),
        )
        await asyncio.to_thread(
            provider.scroll_element,
            app_id=app_id,
            state_id=state_id,
            element_index=required_int_parameter(parameters, "element_index"),
            direction=required_str_parameter(parameters, "direction"),
            pages=required_int_parameter(parameters, "pages"),
        )
    elif command.action == "perform_action":
        reject_unexpected_parameters(
            parameters,
            allowed=frozenset({"app", "state_id", "element_index", "action_name"}),
        )
        await asyncio.to_thread(
            provider.perform_action,
            app_id=app_id,
            state_id=state_id,
            element_index=required_int_parameter(parameters, "element_index"),
            action_name=required_str_parameter(parameters, "action_name"),
        )
    else:
        msg = f"Unsupported semantic desktop action: {command.action}."
        raise DesktopProtocolError(msg)


async def execute_fallback_control(
    provider: DesktopProvider,
    command: DesktopCommand,
    *,
    app_id: str,
    state_id: str,
) -> None:
    """Run one pointer, text, scroll, or key-chord input against the observed app state."""
    parameters = command.parameters
    if command.action in {"click", "double_click"}:
        reject_unexpected_parameters(
            parameters,
            allowed=frozenset({"app", "state_id", "x", "y", "button"}),
        )
        await asyncio.to_thread(
            provider.double_click if command.action == "double_click" else provider.click,
            app_id=app_id,
            state_id=state_id,
            x=required_int_parameter(parameters, "x"),
            y=required_int_parameter(parameters, "y"),
            button=optional_str_parameter(parameters, "button", default="left"),
        )
    elif command.action == "hover":
        reject_unexpected_parameters(parameters, allowed=frozenset({"app", "state_id", "x", "y"}))
        await asyncio.to_thread(
            provider.hover,
            app_id=app_id,
            state_id=state_id,
            x=required_int_parameter(parameters, "x"),
            y=required_int_parameter(parameters, "y"),
        )
    elif command.action == "drag":
        reject_unexpected_parameters(
            parameters,
            allowed=frozenset(
                {
                    "app",
                    "state_id",
                    "start_x",
                    "start_y",
                    "end_x",
                    "end_y",
                    "duration_ms",
                },
            ),
        )
        await asyncio.to_thread(
            provider.drag,
            app_id=app_id,
            state_id=state_id,
            start_x=required_int_parameter(parameters, "start_x"),
            start_y=required_int_parameter(parameters, "start_y"),
            end_x=required_int_parameter(parameters, "end_x"),
            end_y=required_int_parameter(parameters, "end_y"),
            duration_ms=required_int_parameter({"duration_ms": 500, **parameters}, "duration_ms"),
        )
    elif command.action == "type_text":
        reject_unexpected_parameters(parameters, allowed=frozenset({"app", "state_id", "text", "element_index"}))
        targeting = (
            {"element_index": required_int_parameter(parameters, "element_index")}
            if "element_index" in parameters
            else {}
        )
        await asyncio.to_thread(
            provider.type_text,
            app_id=app_id,
            state_id=state_id,
            text=required_str_parameter(parameters, "text"),
            **targeting,
        )
    elif command.action == "scroll":
        reject_unexpected_parameters(
            parameters,
            allowed=frozenset({"app", "state_id", "direction", "pages", "x", "y"}),
        )
        await asyncio.to_thread(
            provider.scroll,
            app_id=app_id,
            state_id=state_id,
            direction=required_str_parameter(parameters, "direction"),
            pages=required_int_parameter(parameters, "pages"),
            x=optional_int_parameter(parameters, "x"),
            y=optional_int_parameter(parameters, "y"),
        )
    elif command.action == "keypress":
        reject_unexpected_parameters(parameters, allowed=frozenset({"app", "state_id", "keys"}))
        await asyncio.to_thread(
            provider.keypress,
            app_id=app_id,
            state_id=state_id,
            keys=_required_str_list_parameter(parameters, "keys"),
        )
    else:
        msg = f"Unsupported fallback desktop action: {command.action}."
        raise DesktopProtocolError(msg)


def _required_str_list_parameter(parameters: dict[str, object], key: str) -> list[str]:
    value = parameters.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        msg = f"Desktop parameter {key} must contain a safe app-local key chord."
        raise DesktopProtocolError(msg)
    try:
        return list(normalize_key_chord(cast("list[str]", value)))
    except ValueError as exc:
        raise DesktopProtocolError(str(exc)) from exc
