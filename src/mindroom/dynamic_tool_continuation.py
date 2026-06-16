"""Same-turn continuation decisions for dynamically loaded tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.models.response import ToolExecution

__all__ = ["DYNAMIC_TOOL_CONTINUATION_LIMIT", "continuation_decision_from_tools"]

DYNAMIC_TOOL_CONTINUATION_LIMIT = 4
_DYNAMIC_TOOL_FUNCTION_NAMES = frozenset({"load_tool", "unload_tool"})
_LOADED_STATUSES = frozenset({"loaded", "already_loaded"})
_UNLOADED_STATUS = "unloaded"


@dataclass(frozen=True)
class _DynamicToolContinuation:
    """One dynamic-tool manager call that stopped the provider loop."""

    function_name: str
    status: str | None
    tool_name: str | None


@dataclass(frozen=True)
class _DynamicToolContinuationDecision:
    """How one response path should handle a completed dynamic-tool call."""

    continuation: _DynamicToolContinuation | None = None
    next_prompt: str | None = None
    limit_message: str | None = None

    @property
    def should_continue(self) -> bool:
        """Return whether the caller should rebuild the agent and run again."""
        return self.next_prompt is not None


def _dynamic_tool_payload(result: object) -> dict[str, object] | None:
    """Return the structured dynamic-tools payload from one tool result."""
    if not isinstance(result, str):
        return None
    try:
        decoded = json.loads(result)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    payload = cast("dict[str, object]", decoded)
    if payload.get("tool") != "dynamic_tools":
        return None
    return payload


def _dynamic_tool_continuation_from_tools(
    tools: Sequence[ToolExecution] | None,
) -> _DynamicToolContinuation | None:
    """Detect a dynamic-tool manager call that stopped before a fresh schema step."""
    for tool in tools or ():
        function_name = tool.tool_name
        if function_name not in _DYNAMIC_TOOL_FUNCTION_NAMES:
            continue

        payload = _dynamic_tool_payload(tool.result)
        if payload is None:
            continue

        status = payload.get("status")
        tool_name = payload.get("tool_name")
        return _DynamicToolContinuation(
            function_name=function_name,
            status=status if isinstance(status, str) else None,
            tool_name=tool_name if isinstance(tool_name, str) else None,
        )
    return None


def _dynamic_tool_continuation_prompt(
    original_prompt: str,
    continuation: _DynamicToolContinuation,
) -> str:
    """Append a hidden continuation notice after one dynamic-tool manager call."""
    tool_part = f" for `{continuation.tool_name}`" if continuation.tool_name else ""
    status_part = f" and returned status `{continuation.status}`" if continuation.status else ""
    if continuation.status in _LOADED_STATUSES:
        continuation_instruction = (
            "After this tool result is processed, MindRoom continues the same task with the updated tool schema. "
            "Continue the same task now and call the loaded tool in a later tool-call step. "
            "Do not wait for another user message."
        )
    elif continuation.status == _UNLOADED_STATUS:
        continuation_instruction = (
            "After this tool result is processed, MindRoom continues the same task with the updated tool schema. "
            "Continue the same task now without the unloaded tool. "
            "Do not wait for another user message."
        )
    else:
        continuation_instruction = (
            "After this tool result is processed, MindRoom continues the same task with the current dynamic tool state. "
            "Continue the same task now using only available tools. "
            "If the requested tool is unavailable, explain that or choose another available deferred tool if useful. "
            "Do not wait for another user message."
        )
    return (
        f"{original_prompt}\n\n"
        "[SYSTEM NOTICE - DYNAMIC TOOL CALL COMPLETED]\n"
        f"The previous model step called `{continuation.function_name}` through dynamic_tools"
        f"{tool_part}{status_part}. "
        f"{continuation_instruction}"
    )


def _dynamic_tool_continuation_limit_message(continuation: _DynamicToolContinuation) -> str:
    """Return visible fallback text when repeated dynamic-tool calls do not converge."""
    tool_part = f" for `{continuation.tool_name}`" if continuation.tool_name else ""
    status_part = f" returned status `{continuation.status}`" if continuation.status else "completed"
    return (
        "Dynamic tool calls did not produce a final answer after "
        f"{DYNAMIC_TOOL_CONTINUATION_LIMIT} continuation attempts. "
        f"The last dynamic tool call was `{continuation.function_name}`{tool_part}, which {status_part}."
    )


def continuation_decision_from_tools(
    tools: Sequence[ToolExecution] | None,
    *,
    original_prompt: str,
    continuation_count: int,
) -> _DynamicToolContinuationDecision:
    """Return the continuation decision for a completed model response."""
    continuation = _dynamic_tool_continuation_from_tools(tools)
    if continuation is None:
        return _DynamicToolContinuationDecision()

    if continuation_count >= DYNAMIC_TOOL_CONTINUATION_LIMIT:
        return _DynamicToolContinuationDecision(
            continuation=continuation,
            limit_message=_dynamic_tool_continuation_limit_message(continuation),
        )

    return _DynamicToolContinuationDecision(
        continuation=continuation,
        next_prompt=_dynamic_tool_continuation_prompt(original_prompt, continuation),
    )
