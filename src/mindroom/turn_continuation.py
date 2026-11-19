"""Same-turn continuation decisions after a thread-model switch or a config reload under a minimal turn."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from mindroom.llm.events import ReportedCall
    from mindroom.llm.runs import ToolExecution

__all__ = ["CONFIG_RELOAD_CALL", "TURN_CONTINUATION_LIMIT", "continuation_decision_from_tools", "stopped_calls"]

TURN_CONTINUATION_LIMIT = 4
_MODEL_SWITCH_FUNCTION_NAME = "switch_thread_model"
# The control call a minimal turn's CLI reports when a config reload refused a call: no tool function has this name.
CONFIG_RELOAD_CALL = "mindroom:config_reload"


@dataclass(frozen=True)
class _TurnContinuation:
    """The attempt that continues a turn: its prompt, and the model a switch for the current response moved it to.

    ``model_name`` is None after a config reload alone, which leaves the turn on its model.
    """

    prompt: str
    model_name: str | None


@dataclass(frozen=True)
class _TurnContinuationDecision:
    """How one response path should handle a model switch or a config reload that stopped its run.

    ``continuation`` is set when the caller should rebuild the agent and run again, and ``limit_message`` when the
    turn spent its continuations and ends with that text instead.
    """

    continuation: _TurnContinuation | None = None
    limit_message: str | None = None


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


def _switched_model(call: ReportedCall) -> str | None:
    """Return the model a switch for the current response moved to, or None when ``call`` made no such switch."""
    if call.tool_name != _MODEL_SWITCH_FUNCTION_NAME:
        return None
    payload = _json_object(call.result)
    if payload is None:
        return None
    model_name = payload.get("model")
    if (
        payload.get("tool") == "thread_model"
        and payload.get("action") == "switch"
        and payload.get("status") == "ok"
        and payload.get("when") == "after-toolcall"
        and isinstance(model_name, str)
    ):
        return model_name
    return None


def _model_switch_continuation_prompt(original_prompt: str, *, model_name: str) -> str:
    """Append the hidden instruction that resumes a stopped model-switch call."""
    return (
        f"{original_prompt}\n\n"
        "[SYSTEM NOTICE - MODEL SWITCH TOOL CALL COMPLETED]\n"
        f"The previous model step switched this thread to `{model_name}` with timing `after-toolcall`. "
        f"Continue the same task with `{model_name}` now. Do not repeat the model switch."
    )


def _model_switch_limit_message(*, model_name: str) -> str:
    """Return visible fallback text when repeated model switches do not converge."""
    return (
        "Model switching did not produce a final answer after "
        f"{TURN_CONTINUATION_LIMIT} continuation attempts. "
        f"The last request selected `{model_name}` with timing `after-toolcall`."
    )


_CONFIG_RELOAD_LIMIT_MESSAGE = (
    "The configuration kept reloading: the reply did not finish after "
    f"{TURN_CONTINUATION_LIMIT} continuation attempts on rebuilt tools."
)


def stopped_calls(tools: Iterable[ToolExecution]) -> list[ReportedCall]:
    """Return the calls among a run's ``tools`` that the run stopped after, for ``continuation_decision_from_tools``.

    Only such a call leaves the turn unanswered; any other call was answered in its run, so the model answered after it.
    """
    return [tool.call for tool in tools if tool.stop_after_call]


def continuation_decision_from_tools(
    tools: Sequence[ReportedCall] | None,
    *,
    original_prompt: str,
    continuation_count: int,
) -> _TurnContinuationDecision:
    """Return the continuation decision for a completed model response.

    ``tools`` are the calls the run stopped after (``stopped_calls``), which the model has not answered, and the
    control calls of a minimal turn's CLI. Only a successful switch for the current response or a config reload
    that refused a call (``CONFIG_RELOAD_CALL``) stops a run; the last switch in provider call order names the model
    the turn continues on. At the continuation limit, the reply ends with its limit message instead.
    """
    switched = [model for model in map(_switched_model, tools or ()) if model is not None]
    if not switched:
        if not any(call.tool_name == CONFIG_RELOAD_CALL for call in tools or ()):
            return _TurnContinuationDecision()
        if continuation_count >= TURN_CONTINUATION_LIMIT:
            return _TurnContinuationDecision(limit_message=_CONFIG_RELOAD_LIMIT_MESSAGE)
        # The refused calls' own results say what happened, so the turn continues on its prompt as it was.
        return _TurnContinuationDecision(continuation=_TurnContinuation(prompt=original_prompt, model_name=None))
    model_name = switched[-1]
    if continuation_count >= TURN_CONTINUATION_LIMIT:
        return _TurnContinuationDecision(limit_message=_model_switch_limit_message(model_name=model_name))
    return _TurnContinuationDecision(
        continuation=_TurnContinuation(
            prompt=_model_switch_continuation_prompt(original_prompt, model_name=model_name),
            model_name=model_name,
        ),
    )
