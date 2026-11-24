"""A decision request's tool policy: ``tool_choice="none"``, which keeps the tool schemas but allows no tool use.

Participation decisions and judgments ask this way and are answered by one JSON object, so an adapter may also turn
off provider-managed tools, or request JSON output, where its provider does not reliably honour a disabled selection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    from mindroom.llm.provider import ToolChoice


def disables_tools(tool_choice: ToolChoice | None) -> bool:
    """Return whether a request with ``tool_choice`` must prevent tool selection and provider-managed execution."""
    return tool_choice == "none"


def disable_tool_selection(request_params: dict[str, Any], tool_choice: ToolChoice | None) -> dict[str, Any]:
    """Apply a decision request's policy to an OpenAI-style request, keeping its tool schemas.

    Tools the authored ``extra_body`` adds get ``tool_choice`` "none" too, since the SDK merges it after the named fields.
    """
    if not disables_tools(tool_choice):
        return request_params
    extra_body = request_params.get("extra_body")
    tools = request_params.get("tools")
    if isinstance(extra_body, dict):
        tools = extra_body.get("tools", tools)
    if not tools:
        return request_params
    request_params["tool_choice"] = "none"
    if isinstance(extra_body, dict) and "tool_choice" in extra_body:
        # SDKs merge extra_body after normal fields. Copy before overriding it.
        request_params["extra_body"] = {**extra_body, "tool_choice": "none"}
    return request_params
