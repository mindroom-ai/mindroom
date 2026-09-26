"""Task-local policy for decision-only requests without tool selection or provider-managed execution."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from typing import Any

_tools_disabled: ContextVar[bool] = ContextVar("provider_tools_disabled", default=False)
_decision_schema: ContextVar[Mapping[str, Any] | None] = ContextVar("provider_decision_schema", default=None)


def provider_tools_disabled() -> bool:
    """Whether the current provider request must prevent tool selection and execution."""
    return _tools_disabled.get()


def decision_response_schema() -> Mapping[str, Any] | None:
    """JSON schema of the current decision's answer, when its caller supplies one."""
    return _decision_schema.get()


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
def without_provider_tools(response_schema: Mapping[str, Any] | None = None) -> Iterator[None]:
    """Make decision-only requests, answered by one JSON object, within this asynchronous task.

    Tool selection and provider-managed execution are disabled. Adapters whose provider does
    not reliably honour disabled tool selection may constrain output to JSON, matching
    ``response_schema`` when the caller supplies one. Prose callers must not use this policy.
    """
    tools_token = _tools_disabled.set(True)
    schema_token = _decision_schema.set(response_schema)
    try:
        yield
    finally:
        _decision_schema.reset(schema_token)
        _tools_disabled.reset(tools_token)
