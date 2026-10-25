"""The final model request of a response that counts toward skill learning, kept for the review to fork.

Like Hermes' review fork, the review replays that request with its request settings unchanged and appends the review
prompt. Its tools are the definitions the run's functions declare, so the provider serves the conversation from its
prompt cache when the request offered those same definitions; the fork of a request that offered a wait budget for
calls that could become jobs differs in its tools and misses the cache. Only the ordinary response path records
requests, and it builds its provider for that one response, so the review owns the provider once the response has
ended.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.agent_loop.model_loop import LoopEndHook, LoopState
    from mindroom.llm.provider import ModelProvider, ModelRequest


@dataclass(frozen=True)
class CapturedRequest:
    """One run's last model request, its messages continued with the model's final answer."""

    model: ModelProvider
    model_name: str
    run_id: str
    request: ModelRequest


@dataclass
class SkillReviewCapture:
    """Keep the final request of a response's latest attempt; a review forks it only for the response's final run."""

    latest: CapturedRequest | None = None


def capture_final_request(
    capture: SkillReviewCapture,
    model: ModelProvider,
    *,
    model_name: str,
) -> LoopEndHook:
    """Return the loop-end hook that records one primary attempt's final request for the skill review.

    Each attempt names its own configured ``model_name``, because a same-turn continuation can switch models within one
    response. The request keeps the run's own messages and tool definitions, as its functions declare them: what a
    pre-request hook projected for that request alone, such as the approval receipt or the wait budget job admission
    accepts, belongs to the run's own calls, and the review answers its calls with other functions.
    """

    def record(state: LoopState, request: ModelRequest) -> None:
        # The run keeps using its messages after the loop, so copies keep the conversation exactly as it ended.
        messages = tuple(copy.copy(message) for message in state.messages)
        capture.latest = CapturedRequest(
            model=model,
            model_name=model_name,
            run_id=state.run.run_id,
            request=replace(request, messages=messages, tools=state.offered.specs()),
        )

    return record


__all__ = ["CapturedRequest", "SkillReviewCapture", "capture_final_request"]
