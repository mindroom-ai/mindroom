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
_MODEL_SWITCH_FUNCTION_NAME = "switch_thread_model"
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
    model_switch_name: str | None = None
    model_switch_when: str | None = None

    @property
    def should_continue(self) -> bool:
        """Return whether the caller should rebuild the agent and run again."""
        return self.next_prompt is not None


def _json_object(result: object) -> dict[str, object] | None:
    """Decode one tool result as a JSON object."""
    if not isinstance(result, str):
        return None
    try:
        decoded = json.loads(result)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return cast("dict[str, object]", decoded)


def _dynamic_tool_payload(result: object) -> dict[str, object] | None:
    """Return the structured dynamic-tools payload from one tool result."""
    payload = _json_object(result)
    if payload is None:
        return None
    if payload.get("tool") != "dynamic_tools":
        return None
    return payload


def _model_switch_from_tools(
    tools: Sequence[ToolExecution] | None,
) -> tuple[str | None, str | None] | None:
    """Return the last successful thread-model switch in provider call order."""
    switch_seen = False
    for tool in reversed(tools or ()):
        if tool.tool_name != _MODEL_SWITCH_FUNCTION_NAME:
            continue
        switch_seen = True
        payload = _json_object(tool.result)
        if payload is None:
            continue
        model_name = payload.get("model")
        when = payload.get("when")
        if (
            payload.get("tool") == "thread_model"
            and payload.get("action") == "switch"
            and payload.get("status") == "ok"
            and isinstance(model_name, str)
            and when in {"after-toolcall", "next-turn"}
        ):
            return model_name, cast("str", when)
    return (None, None) if switch_seen else None


def _model_switch_continuation_prompt(original_prompt: str, *, model_name: str, when: str) -> str:
    """Append the hidden instruction that resumes a stopped model-switch call."""
    instruction = (
        f"Continue the same task with `{model_name}` now. Do not repeat the model switch."
        if when == "after-toolcall"
        else (
            "Continue the same task with the model that started this response. "
            f"The persisted `{model_name}` selection begins on the next user turn. "
            "Do not repeat the model switch."
        )
    )
    return (
        f"{original_prompt}\n\n"
        "[SYSTEM NOTICE - MODEL SWITCH TOOL CALL COMPLETED]\n"
        f"The previous model step switched this thread to `{model_name}` with timing `{when}`. "
        f"{instruction}"
    )


def _model_switch_limit_message(*, model_name: str, when: str) -> str:
    """Return visible fallback text when repeated model switches do not converge."""
    return (
        "Model switching did not produce a final answer after "
        f"{DYNAMIC_TOOL_CONTINUATION_LIMIT} continuation attempts. "
        f"The last request selected `{model_name}` with timing `{when}`."
    )


def _failed_model_switch_continuation_prompt(original_prompt: str) -> str:
    """Resume after a rejected stop-after-tool model switch."""
    return (
        f"{original_prompt}\n\n"
        "[SYSTEM NOTICE - MODEL SWITCH TOOL CALL FAILED]\n"
        "The previous model-switch request was rejected. "
        "Continue the same task with the current model and do not repeat the invalid switch."
    )


def _model_switch_decision(
    model_switch: tuple[str | None, str | None],
    *,
    original_prompt: str,
    continuation_count: int,
) -> _DynamicToolContinuationDecision:
    """Return how to continue after one thread-model switch result."""
    model_name, when = model_switch
    if model_name is None or when is None:
        if continuation_count >= DYNAMIC_TOOL_CONTINUATION_LIMIT:
            return _DynamicToolContinuationDecision(
                limit_message="Model switching did not produce a final answer after repeated rejected requests.",
            )
        return _DynamicToolContinuationDecision(
            next_prompt=_failed_model_switch_continuation_prompt(original_prompt),
        )
    if continuation_count >= DYNAMIC_TOOL_CONTINUATION_LIMIT:
        return _DynamicToolContinuationDecision(
            limit_message=_model_switch_limit_message(model_name=model_name, when=when),
            model_switch_name=model_name,
            model_switch_when=when,
        )
    return _DynamicToolContinuationDecision(
        next_prompt=_model_switch_continuation_prompt(
            original_prompt,
            model_name=model_name,
            when=when,
        ),
        model_switch_name=model_name,
        model_switch_when=when,
    )


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
    model_switch = _model_switch_from_tools(tools)
    if model_switch is not None:
        return _model_switch_decision(
            model_switch,
            original_prompt=original_prompt,
            continuation_count=continuation_count,
        )

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
