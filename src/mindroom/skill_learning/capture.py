"""The final model request of a response that counts toward skill learning, kept for the review to fork.

Like Hermes' review fork, the review replays that request with its tools unchanged and appends the review prompt,
so the provider serves the conversation from its prompt cache. Only the ordinary response path records requests, and
it builds its provider for that one response, so the review owns the provider once the response has ended.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from pydantic import BaseModel

    from mindroom.agent_loop.model_loop import LoopEndHook, LoopState
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider, ModelRequest, ToolChoice, ToolSpec


@dataclass(frozen=True)
class CapturedRequest:
    """One run's last model request, whose messages end with the model's final answer."""

    model: ModelProvider
    model_name: str
    run_id: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    tool_choice: ToolChoice | None
    response_format: type[BaseModel] | Mapping[str, Any] | None


@dataclass
class SkillReviewCapture:
    """Keep the final request of a response's latest attempt; a review forks it only for the response's final run."""

    latest: CapturedRequest | None = None


def capture_final_request(
    capture: SkillReviewCapture,
    model: ModelProvider,
    *,
    run_id: str,
    model_name: str,
) -> LoopEndHook:
    """Return the loop-end hook that records one primary attempt's final request for the skill review.

    Each attempt names its own configured ``model_name``, because a same-turn continuation can switch models within one
    response.
    """

    def record(state: LoopState, request: ModelRequest) -> None:
        capture.latest = CapturedRequest(
            model=model,
            model_name=model_name,
            run_id=run_id,
            # The run keeps using these messages after the loop, so copies keep the request exactly as sent.
            messages=tuple(copy.copy(message) for message in state.messages),
            tools=tuple(request.tools),
            tool_choice=request.tool_choice,
            response_format=request.response_format,
        )

    return record


__all__ = ["CapturedRequest", "SkillReviewCapture", "capture_final_request"]
