"""Answer the tool calls of a response the output limit cut off, and stop the run when it keeps happening.

A response stopped at the model's output limit is incomplete, so a tool call in it may be partial or missing, and
partial arguments may still parse; none of its calls may run. Each call is answered with a result telling the model why,
so the next request can split the work; the loop's third cut-off response ends the run with an error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_loop.tools import failed_answer
from mindroom.llm.errors import ModelOutputLimitError
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.agent_loop.model_loop import AnswerToolCalls
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import StopReason

logger = get_logger(__name__)

# Every cut-off response spends the model's whole output budget, so a run gets this many corrective results and the
# next cut-off ends it.
_OUTPUT_LIMIT_RETRIES = 2
CUT_OFF_TOOL_CALL_RESULT = (
    "Not run: your previous response hit the output limit and was cut off before it was complete, "
    "so none of its tool calls ran. Split large content across several smaller tool calls, "
    "for example by writing a long file in parts, then continue."
)


def output_limit_policy() -> AnswerToolCalls:
    """Return the ``answer_tool_calls`` hook of one run, counting that run's cut-off responses."""
    cut_off_responses = 0

    def answer(message: Message, stop_reason: StopReason | None) -> list[Message] | None:
        nonlocal cut_off_responses
        if stop_reason != "output_limit":
            return None
        cut_off_responses += 1
        if cut_off_responses > _OUTPUT_LIMIT_RETRIES:
            msg = (
                f"The output limit cut off {cut_off_responses} of this model's responses in this run "
                "before their tool calls were complete, so the run stopped. Ask for the work in smaller steps, "
                "or raise the model's output token limit."
            )
            raise ModelOutputLimitError(msg)
        logger.warning("tool_calls_cut_off_by_output_limit", cut_off_responses=cut_off_responses)
        return [
            failed_answer(call.get("id"), (call.get("function") or {}).get("name"), CUT_OFF_TOOL_CALL_RESULT)
            for call in message.tool_calls or []
        ]

    return answer


__all__ = ["CUT_OFF_TOOL_CALL_RESULT", "output_limit_policy"]
