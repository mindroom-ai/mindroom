"""Agent tool for a pinned Matrix-attached desktop device."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import uuid4

from agno.media import Image
from agno.tools import Toolkit
from agno.tools.function import ToolResult

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.custom_tools.toolkit_functions import register_toolkit_functions
from mindroom.desktop.client import DesktopRequestError, desktop_response_router
from mindroom.desktop.media import DesktopMediaError, download_encrypted_screenshot
from mindroom.desktop.protocol import (
    DESKTOP_CONTROL_ACTIONS,
    MAX_COMMAND_TTL_MS,
    DesktopCommand,
    DesktopProtocolError,
    DesktopResponse,
)
from mindroom.matrix.olm_to_device import OlmToDeviceError, PinnedMatrixDevice
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    import nio

_ACTION_SCHEMA = {
    "type": "string",
    "enum": ["status", "screenshot", "click", "type_text", "scroll", "keypress"],
    "description": "Desktop operation to perform.",
}
_DESKTOP_PARAMETERS: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": _ACTION_SCHEMA,
        "x": {"type": "integer", "description": "Screen x coordinate for click or optional scroll position."},
        "y": {"type": "integer", "description": "Screen y coordinate for click or optional scroll position."},
        "button": {"type": "string", "enum": ["left", "middle", "right"], "default": "left"},
        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
        "clicks": {"type": "integer", "minimum": -50, "maximum": 50},
        "keys": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 4,
            "description": "One key or a short key combination, such as ['command', 'l'].",
        },
    },
    "required": ["action"],
}


class DesktopTools(Toolkit):
    """Operate one exact local desktop through short-lived encrypted Matrix commands."""

    def __init__(
        self,
        device_user_id: str,
        device_id: str,
        device_ed25519: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(name="desktop")
        if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= MAX_COMMAND_TTL_MS / 1000:
            msg = f"timeout_seconds must be between 1 and {MAX_COMMAND_TTL_MS // 1000}."
            raise ValueError(msg)
        self._target = PinnedMatrixDevice(
            user_id=device_user_id,
            device_id=device_id,
            ed25519=device_ed25519,
        )
        self._timeout_seconds = float(timeout_seconds)
        register_toolkit_functions(
            self,
            sync_entrypoints={},
            async_entrypoints={"desktop": self.desktop},
            descriptions={
                "desktop": (
                    "Observe or operate the configured local primary screen. Start with status or screenshot. "
                    "Control actions normally return a fresh screenshot; inspect it before the next action. "
                    "Coordinates refer to the reported source screen dimensions; scale from the image dimensions when they differ. "
                    "If an action outcome is unknown or its follow-up screenshot fails, never repeat it automatically. "
                    "Local policy may keep the bridge observe-only or reject an expired control lease. "
                    "Never use type_text for passwords, tokens, or other secrets."
                ),
            },
            parameters={"desktop": _DESKTOP_PARAMETERS},
        )

    async def desktop(
        self,
        action: str,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        text: str | None = None,
        clicks: int | None = None,
        keys: list[str] | None = None,
    ) -> ToolResult:
        """Run one desktop action and return structured state plus an optional screenshot."""
        context = get_tool_runtime_context()
        if context is None:
            return _error_result(action, "Desktop tool requires a live Matrix runtime context.")
        try:
            parameters = _action_parameters(
                action,
                x=x,
                y=y,
                button=button,
                text=text,
                clicks=clicks,
                keys=keys,
            )
            now_ms = round(time.time() * 1000)
            command = DesktopCommand(
                request_id=uuid4().hex,
                session_id=context.session_id,
                sequence=time.time_ns(),
                issued_at_ms=now_ms,
                expires_at_ms=now_ms + round(self._timeout_seconds * 1000),
                action=action,  # ty: ignore[invalid-argument-type] - validated by _action_parameters.
                requester_id=context.requester_id,
                agent_name=context.agent_name,
                parameters=parameters,
            )
            response = await desktop_response_router(context.client).request(
                self._target,
                command,
                timeout_seconds=self._timeout_seconds,
            )
            return await _tool_result_from_response(
                action,
                client=context.client,
                response=response,
            )
        except (DesktopMediaError, DesktopProtocolError, DesktopRequestError, OlmToDeviceError, ValueError) as exc:
            return _error_result(action, str(exc))


async def _tool_result_from_response(action: str, *, client: nio.AsyncClient, response: object) -> ToolResult:
    if not isinstance(response, DesktopResponse):
        msg = "Desktop device returned an invalid response."
        raise DesktopProtocolError(msg)
    if not response.ok:
        return _error_result(action, response.error or "Desktop device rejected the request.")
    content = custom_tool_payload(
        "desktop",
        "ok",
        action=action,
        result=response.result,
    )
    if response.screenshot is None:
        return _result_without_screenshot(action, response=response, content=content)
    try:
        image_bytes = await download_encrypted_screenshot(client, response.screenshot)
    except DesktopMediaError:
        if action not in DESKTOP_CONTROL_ACTIONS:
            raise
        return _partial_result(
            action,
            result=response.result,
            message=(
                "The desktop action completed, but its follow-up screenshot could not be decrypted; "
                "do not repeat the action automatically. Request status or a screenshot before deciding the next step."
            ),
        )
    return ToolResult(
        content=content,
        images=[Image(content=image_bytes, mime_type=response.screenshot.mime_type)],
    )


def _result_without_screenshot(action: str, *, response: DesktopResponse, content: str) -> ToolResult:
    if action == "status":
        return ToolResult(content=content)
    action_may_have_run = (
        response.result.get("action_completed") is True or response.result.get("action_outcome") == "unknown"
    )
    if action in DESKTOP_CONTROL_ACTIONS and action_may_have_run:
        return _partial_result(
            action,
            result=response.result,
            message=_partial_warning(response.result),
        )
    return _error_result(action, "Desktop response did not include the required screenshot.")


def _action_parameters(
    action: str,
    *,
    x: int | None,
    y: int | None,
    button: str,
    text: str | None,
    clicks: int | None,
    keys: list[str] | None,
) -> dict[str, object]:
    if action in {"status", "screenshot"}:
        return {}
    if action == "click":
        if x is None or y is None:
            msg = "click requires x and y coordinates."
            raise ValueError(msg)
        return {"x": x, "y": y, "button": button}
    if action == "type_text":
        if text is None:
            msg = "type_text requires text."
            raise ValueError(msg)
        return {"text": text}
    if action == "scroll":
        return _scroll_parameters(clicks=clicks, x=x, y=y)
    if action == "keypress":
        if keys is None:
            msg = "keypress requires keys."
            raise ValueError(msg)
        return {"keys": keys}
    msg = f"Unsupported desktop action: {action}."
    raise ValueError(msg)


def _scroll_parameters(*, clicks: int | None, x: int | None, y: int | None) -> dict[str, object]:
    if clicks is None:
        msg = "scroll requires clicks."
        raise ValueError(msg)
    parameters: dict[str, object] = {"clicks": clicks}
    if x is None and y is None:
        return parameters
    if x is None or y is None:
        msg = "scroll x and y must be supplied together."
        raise ValueError(msg)
    parameters.update({"x": x, "y": y})
    return parameters


def _error_result(action: str, message: str) -> ToolResult:
    return ToolResult(
        content=custom_tool_payload(
            "desktop",
            "error",
            action=action,
            message=message,
        ),
    )


def _partial_warning(result: dict[str, object]) -> str:
    warning = result.get("warning")
    if isinstance(warning, str) and warning:
        return warning
    return (
        "The desktop action completed without a follow-up screenshot; do not repeat it automatically. "
        "Request status or a screenshot before deciding the next step."
    )


def _partial_result(action: str, *, result: dict[str, object], message: str) -> ToolResult:
    return ToolResult(
        content=custom_tool_payload(
            "desktop",
            "partial",
            action=action,
            result=result,
            message=message,
        ),
    )


__all__ = ["DesktopTools"]
