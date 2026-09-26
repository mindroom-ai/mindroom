"""The final model request of a response that counts toward skill learning, kept for the review to fork.

Like Hermes' review fork, the review replays that request with its tools unchanged and appends the review prompt,
so the provider serves the conversation from its prompt cache. Only the ordinary response path records requests, and
it builds its agent and model for that one response, so the review owns the model once the response has ended.
"""

from __future__ import annotations

import copy
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from agno.run.agent import RunOutput

from mindroom.agno_compat_model_hooks import temporary_response_observer

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from agno.models.base import Model
    from agno.models.message import Message
    from agno.tools.function import Function
    from pydantic import BaseModel


@dataclass(frozen=True)
class CapturedRequest:
    """One response loop's last model request, whose messages end with the model's final answer."""

    model: Model
    model_name: str
    run_id: str
    messages: tuple[Message, ...]
    tools: tuple[Function | dict[str, Any], ...]
    tool_choice: str | dict[str, Any] | None
    response_format: dict[str, Any] | type[BaseModel] | None


@dataclass
class SkillReviewCapture:
    """Keep the final request of a response's latest attempt; a review forks it only for the response's final run."""

    latest: CapturedRequest | None = None

    def observe(self, model: Model | None, *, run_id: str, model_name: str) -> AbstractContextManager[None]:
        """Record the attempt's response loops on ``model``, the configured ``model_name``, while the attempt runs.

        Each attempt names its own model, because a dynamic continuation can switch models within one response.
        """
        if model is None:
            return nullcontext()

        def record(kwargs: dict[str, object]) -> None:
            run_response = kwargs.get("run_response")
            messages = kwargs.get("messages")
            if (
                not isinstance(run_response, RunOutput)
                or run_response.run_id != run_id
                or not isinstance(messages, list)
            ):
                return
            self.latest = CapturedRequest(
                model=model,
                model_name=model_name,
                run_id=run_id,
                # The run keeps using these messages after the loop, so copies keep the request exactly as sent.
                messages=tuple(copy.copy(message) for message in cast("list[Message]", messages)),
                tools=tuple(cast("list[Function | dict[str, Any]]", kwargs.get("tools") or [])),
                tool_choice=cast("str | dict[str, Any] | None", kwargs.get("tool_choice")),
                response_format=cast("dict[str, Any] | type[BaseModel] | None", kwargs.get("response_format")),
            )

        return temporary_response_observer(model, record)


def observe_final_request(
    capture: SkillReviewCapture | None,
    model: Model | None,
    *,
    run_id: str,
    model_name: str,
) -> AbstractContextManager[None]:
    """Record one primary attempt's final request when its response counts toward skill learning."""
    return capture.observe(model, run_id=run_id, model_name=model_name) if capture is not None else nullcontext()
