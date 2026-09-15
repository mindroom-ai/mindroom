"""Task-local policy preventing tool selection and provider-managed execution."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

_tools_disabled: ContextVar[bool] = ContextVar("provider_tools_disabled", default=False)


def provider_tools_disabled() -> bool:
    """Whether the current provider request must prevent tool selection and execution."""
    return _tools_disabled.get()


def disable_tool_selection(request_params: dict[str, Any]) -> dict[str, Any]:
    """Apply the task policy to an OpenAI-style request, preserving tool schemas."""
    if not provider_tools_disabled():
        return request_params
    extra_body = request_params.get("extra_body")
    effective_tools = (
        extra_body.get("tools", request_params.get("tools"))
        if isinstance(extra_body, dict)
        else request_params.get("tools")
    )
    if not effective_tools:
        return request_params
    request_params["tool_choice"] = "none"
    if isinstance(extra_body, dict) and "tool_choice" in extra_body:
        # SDKs merge extra_body after normal fields. Copy before overriding it.
        request_params["extra_body"] = {**extra_body, "tool_choice": "none"}
    return request_params


@contextmanager
def without_provider_tools() -> Iterator[None]:
    """Disable tool selection and provider-managed execution within this asynchronous task."""
    token = _tools_disabled.set(True)
    try:
        yield
    finally:
        _tools_disabled.reset(token)
