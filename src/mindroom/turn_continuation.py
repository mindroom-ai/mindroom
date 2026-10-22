"""Same-turn continuation decisions after a thread-model switch."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.models.response import ToolExecution

__all__ = ["TURN_CONTINUATION_LIMIT", "continuation_decision_from_tools"]

TURN_CONTINUATION_LIMIT = 4
_MODEL_SWITCH_FUNCTION_NAME = "switch_thread_model"


@dataclass(frozen=True)
class _TurnContinuationDecision:
    """How one response path should handle a completed model-switch call."""

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
        f"{TURN_CONTINUATION_LIMIT} continuation attempts. "
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
) -> _TurnContinuationDecision:
    """Return how to continue after one thread-model switch result."""
    model_name, when = model_switch
    if model_name is None or when is None:
        if continuation_count >= TURN_CONTINUATION_LIMIT:
            return _TurnContinuationDecision(
                limit_message="Model switching did not produce a final answer after repeated rejected requests.",
            )
        return _TurnContinuationDecision(
            next_prompt=_failed_model_switch_continuation_prompt(original_prompt),
        )
    if continuation_count >= TURN_CONTINUATION_LIMIT:
        return _TurnContinuationDecision(
            limit_message=_model_switch_limit_message(model_name=model_name, when=when),
            model_switch_name=model_name,
            model_switch_when=when,
        )
    return _TurnContinuationDecision(
        next_prompt=_model_switch_continuation_prompt(
            original_prompt,
            model_name=model_name,
            when=when,
        ),
        model_switch_name=model_name,
        model_switch_when=when,
    )


def continuation_decision_from_tools(
    tools: Sequence[ToolExecution] | None,
    *,
    original_prompt: str,
    continuation_count: int,
) -> _TurnContinuationDecision:
    """Return the continuation decision for a completed model response."""
    model_switch = _model_switch_from_tools(tools)
    if model_switch is None:
        return _TurnContinuationDecision()
    return _model_switch_decision(
        model_switch,
        original_prompt=original_prompt,
        continuation_count=continuation_count,
    )
